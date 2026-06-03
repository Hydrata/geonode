#########################################################################
#
# Copyright (C) 2020 OSGeo
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
#########################################################################
import logging

from dynamic_rest.fields.fields import DynamicRelationField
from dynamic_rest.serializers import DynamicModelSerializer
from rest_framework import serializers
from rest_framework.exceptions import ParseError, ValidationError

from geonode.base.api.serializers import (
    BULK_PERMS_CONTEXT_KEY,
    DetailUrlField,
    BaseDynamicModelSerializer,
    ResourceBaseSerializer,
    LinksSerializer,
    ExtentBboxField,
)
from geonode.layers.api.serializers import FeatureInfoTemplateField, StyleSerializer
from geonode.layers.models import Dataset
from geonode.maps.models import Map, MapLayer
from geonode.security.registry import permissions_registry

logger = logging.getLogger(__name__)


class DynamicFullyEmbedM2MRelationField(DynamicRelationField):
    def __init__(self, serializer_class, queryset=None, sideloading=None, debug=False, **kwargs):
        kwargs["queryset"] = queryset
        kwargs["sideloading"] = sideloading
        kwargs["debug"] = debug
        # Assures embed and many are always true
        kwargs["many"] = True
        kwargs["embed"] = True
        super(DynamicFullyEmbedM2MRelationField, self).__init__(serializer_class, **kwargs)

    def to_internal_value_single(self, data, serializer):
        """Return the underlying object, given the serialized form."""
        related_model = serializer.Meta.model
        instance = None

        # When updating a Map element, it's possible to update or create new m2m elements
        if self.root_serializer.instance and ("pk" in data or "id" in data):
            instance_pk = data["pk"] if "pk" in data else data["id"]
            # Get object
            if instance_pk is not None:
                try:
                    instance = related_model.objects.get(pk=instance_pk)
                except related_model.DoesNotExist:
                    raise ValidationError(
                        f"Invalid value for '{self.field_name}': {related_model.__name__} object with ID={data} not found"
                    )

        # If we found a instance, we should update it instead of creating a new one
        if instance and not serializer.instance:
            serializer.instance = instance

        # Save object
        serializer.is_valid(raise_exception=True)
        instance = serializer.save()

        return instance

    def to_internal_value(self, data):
        """Return the underlying object(s), given the serialized form."""
        if not isinstance(data, list):
            raise ParseError(f"'{self.field_name}' value must be a list")

        instance_list = []
        instance_pk_list = []
        for instance_data in data:
            if isinstance(instance_data, self.serializer_class.Meta.model):
                return instance_data
            serializer = self.get_serializer(data=instance_data, many=False)
            instance = self.to_internal_value_single(instance_data, serializer)
            instance_list.append(instance)
            instance_pk_list.append(instance.pk)

        # Delete removed instances
        if self.root_serializer.instance:
            m2m_field_manager = getattr(self.root_serializer.instance, self.field_name)
            m2m_field_manager.exclude(pk__in=instance_pk_list).delete()

        return instance_list


class MapLayerDatasetSerializer(DynamicModelSerializer):
    default_style = DynamicRelationField(StyleSerializer, embed=True, many=False, read_only=True)
    styles = DynamicRelationField(StyleSerializer, embed=True, many=True, read_only=True)
    featureinfo_custom_template = FeatureInfoTemplateField(deferred=True)
    extent = ExtentBboxField(required=False)

    perms = serializers.SerializerMethodField(read_only=True)
    links = DynamicRelationField(LinksSerializer, source="id", read_only=True, deferred=False)

    class Meta:
        model = Dataset
        name = "dataset"
        fields = (
            "alternate",
            "featureinfo_custom_template",
            "title",
            "perms",
            "links",
            "pk",
            "has_time",
            "default_style",
            "styles",
            "ptype",
            "extent",
        )

    def get_perms(self, instance):
        """
        Returns the permissions for the dataset instance using  cache.

        On the map-blob path the perms are pre-computed in one bulk pass by
        ``MapSerializer.to_representation`` and injected via
        ``context['_bulk_layer_perms']`` (a ``{dataset_pk: perms}`` dict); when
        that is present we read from it, otherwise we fall back to the
        per-resource cached lookup.
        """
        request = self.context.get("request")
        if not (request and request.user and instance):
            return []
        bulk_layer_perms = self.context.get(BULK_PERMS_CONTEXT_KEY)
        if bulk_layer_perms is not None and instance.pk in bulk_layer_perms:
            return bulk_layer_perms[instance.pk]
        return permissions_registry.get_perms(instance=instance, user=request.user, use_cache=True)


class MapLayerSerializer(DynamicModelSerializer):
    dataset = DynamicRelationField(MapLayerDatasetSerializer, embed=True)

    class Meta:
        model = MapLayer
        name = "maplayer"
        fields = (
            "pk",
            "extra_params",
            "current_style",
            "dataset",
            "name",
            "order",
            "visibility",
            "opacity",
        )


class SimpleMapLayerSerializer(serializers.ModelSerializer):
    class Meta:
        model = MapLayer
        name = "maplayer"
        fields = ("pk", "name", "extra_params", "current_style", "order", "visibility", "opacity")


class MapSerializer(ResourceBaseSerializer):
    maplayers = DynamicFullyEmbedM2MRelationField(MapLayerSerializer, deferred=False)

    def to_representation(self, instance):
        """
        Pre-compute every embedded layer's permissions in ONE bulk pass before the
        nested ``MapLayerDatasetSerializer`` instances run, eliminating the per-layer
        Guardian N+1 on the cold map-blob path. The result is injected into the shared
        serializer context as ``{dataset_pk: perms}``; each nested ``get_perms`` reads
        it (falling back to the per-resource cached lookup if absent).
        """
        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None
        if user is not None:
            # select_related("dataset") so materialising the layers' datasets doesn't
            # itself fan out into a per-layer FK query (a local fix; the task forbids
            # prefetch_related on the MapViewSet queryset). Dedup by pk (order-preserving).
            datasets = {
                maplayer.dataset.pk: maplayer.dataset
                for maplayer in instance.maplayers.select_related("dataset").all()
                if maplayer.dataset is not None
            }
            if datasets:
                self.context[BULK_PERMS_CONTEXT_KEY] = permissions_registry.get_perms_bulk(
                    list(datasets.values()), user=user, use_cache=True
                )
        return super().to_representation(instance)

    class Meta:
        model = Map
        name = "map"
        view_name = "maps-list"
        fields = list(
            set(
                ResourceBaseSerializer.Meta.fields
                + (
                    "uuid",
                    "urlsuffix",
                    "featuredurl",
                    "data",
                    "maplayers",
                )
            )
        )


class SimpleMapSerializer(BaseDynamicModelSerializer):
    detail_url = DetailUrlField(read_only=True)

    class Meta:
        model = Map
        name = "map"
        view_name = "maps-list"
        fields = (
            "pk",
            "title",
            "detail_url",
        )
