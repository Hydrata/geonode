#########################################################################
#
# Copyright (C) 2021 OSGeo
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
import hashlib
import logging
from uuid import uuid4

from django.core.cache import cache
from django.db import transaction
from drf_spectacular.utils import extend_schema
from dynamic_rest.filters import DynamicFilterBackend, DynamicSortingFilter
from dynamic_rest.viewsets import DynamicModelViewSet
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticatedOrReadOnly
from rest_framework.response import Response

from geonode.base import register_event
from geonode.base.api.filters import DynamicSearchFilter, ExtentFilter
from geonode.base.api.mixins import AdvertisedListMixin
from geonode.base.api.pagination import GeoNodeApiPagination
from geonode.base.api.permissions import UserHasPerms
from geonode.base.api.views import ApiPresetsInitializer
from geonode.base.enumerations import EventType
from geonode.layers.api.serializers import DatasetSerializer
from geonode.maps.api.exception import GeneralMapsException
from geonode.maps.api.permissions import MapPermissionsFilter
from geonode.maps.api.serializers import MapLayerSerializer, MapSerializer
from geonode.maps.contants import _PERMISSION_MSG_SAVE
from geonode.maps.models import Map
from geonode.maps.signals import map_changed_signal
from geonode.resource.manager import resource_manager
from geonode.utils import resolve_object

MAP_RETRIEVE_CACHE_TTL = 1800  # seconds (TASK-666 H-S8 PoC: 120 → 1800)

logger = logging.getLogger(__name__)


class MapViewSet(ApiPresetsInitializer, DynamicModelViewSet, AdvertisedListMixin):
    """
    API endpoint that allows maps to be viewed or edited.
    """

    http_method_names = ["get", "patch", "post", "put"]
    permission_classes = [
        IsAuthenticatedOrReadOnly,
        UserHasPerms(perms_dict={"default": {"POST": ["base.add_resourcebase"]}}),
    ]
    filter_backends = [
        DynamicFilterBackend,
        DynamicSortingFilter,
        DynamicSearchFilter,
        ExtentFilter,
        MapPermissionsFilter,
    ]
    queryset = Map.objects.all().order_by("-created")
    serializer_class = MapSerializer
    pagination_class = GeoNodeApiPagination

    def list(self, request, *args, **kwargs):
        # Avoid overfetching removing mapslayer of the list.
        request.query_params.add("exclude[]", "maplayers")
        return super(MapViewSet, self).list(request, *args, **kwargs)

    def retrieve(self, request, *args, **kwargs):
        cache_key = self._retrieve_cache_key(request, kwargs.get("pk"))
        cached = cache.get(cache_key) if cache_key else None
        if cached is not None:
            response = Response(cached)
            response["Cache-Control"] = "private, max-age=300"
            response["X-Cache"] = "HIT"
            return response

        response = super(MapViewSet, self).retrieve(request, *args, **kwargs)
        response["Cache-Control"] = "private, max-age=300"
        response["X-Cache"] = "MISS"

        if cache_key and response.status_code == 200:
            cache.set(cache_key, response.data, MAP_RETRIEVE_CACHE_TTL)

        return response

    @staticmethod
    def _retrieve_cache_key(request, pk):
        if not pk:
            return None
        user_id = request.user.pk if request.user.is_authenticated else 0
        qs = request.META.get("QUERY_STRING", "")
        qs_hash = hashlib.md5(qs.encode()).hexdigest()[:12]
        # Include a version number so we can invalidate all keys for a map
        version = cache.get(f"map_v_{pk}") or 0
        return f"map_api_{pk}_v{version}_{user_id}_{qs_hash}"

    @staticmethod
    def invalidate_map_cache(map_pk):
        """Bump version to invalidate all cached responses for this map.

        Two correctness requirements forced the current shape:

        (1) The version_key MUST live at least as long as any blob entry
            indexed under it. Default Django cache TTL is 300s; blob
            entries cache at MAP_RETRIEVE_CACHE_TTL (1800s). If the
            version_key expires first, the lookup falls back to v0 →
            still-alive v0 blob entry from a much earlier write → stale
            blob served for up to (MAP_RETRIEVE_CACHE_TTL − default_ttl)
            after the version expires. We refresh the version_key TTL
            on every invalidation, sized to outlive any blob entry it
            namespaces.

        (2) Callers MUST schedule this via transaction.on_commit when
            invalidating from inside a write transaction; otherwise a
            concurrent GET landing between the cache-version bump and
            the DB commit will read the OLD blob (READ COMMITTED hides
            the uncommitted write), cache it under the NEW version key,
            and serve it for MAP_RETRIEVE_CACHE_TTL.
        """
        version_key = f"map_v_{map_pk}"
        try:
            new_version = cache.incr(version_key)
        except ValueError:
            new_version = 1
        # incr preserves the original TTL; re-set so this version_key
        # outlives the longest-lived blob entry under it.
        cache.set(version_key, new_version, MAP_RETRIEVE_CACHE_TTL)

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        """
        Changes in the m2m `maplayers` are committed before object changes.
        To protect the db, this action is done within an atomic tansation.
        """
        return super(MapViewSet, self).update(request, *args, **kwargs)

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        """
        Changes in the m2m `maplayers` are committed before object changes.
        To protect the db, this action is done within an atomic tansation.
        """
        return super(MapViewSet, self).create(request, *args, **kwargs)

    @extend_schema(
        methods=["get"],
        responses={200: MapLayerSerializer(many=True)},
        description="API endpoint allowing to retrieve the MapLayers list.",
    )
    @action(detail=True, methods=["get"])
    def maplayers(self, request, pk=None, *args, **kwargs):
        map = self.get_object()
        resources = map.maplayers
        return Response(MapLayerSerializer(embed=True, many=True).to_representation(resources))

    @extend_schema(
        methods=["get"],
        responses={200: DatasetSerializer(many=True)},
        description="API endpoint allowing to retrieve the local MapLayers.",
    )
    @action(detail=True, methods=["get"])
    def datasets(self, request, pk=None, *args, **kwargs):
        map = self.get_object()
        resources = map.datasets
        return Response(DatasetSerializer(embed=True, many=True).to_representation(resources))

    def perform_create(self, serializer):
        # Thumbnail will be handled later
        post_creation_data = {"thumbnail": serializer.validated_data.pop("thumbnail_url", "")}

        instance = serializer.save(
            owner=self.request.user,
            resource_type="map",
            uuid=str(uuid4()),
        )

        # events and resouce routines
        self._post_change_routines(
            instance=instance,
            create_action_perfomed=True,
            additional_data=post_creation_data,
        )

        # Handle thumbnail generation
        resource_manager.set_thumbnail(instance.uuid, instance=instance, overwrite=False)

    def perform_update(self, serializer):
        # Check instance permissions with resolve_object
        mapid = serializer.instance.id
        key = "urlsuffix" if Map.objects.filter(urlsuffix=mapid).exists() else "pk"
        map_obj = resolve_object(
            self.request, Map, {key: mapid}, permission="base.change_resourcebase", permission_msg=_PERMISSION_MSG_SAVE
        )
        instance = serializer.instance
        if instance != map_obj:
            raise GeneralMapsException(detail="serializer instance and object are different")
        # Thumbnail will be handled later
        post_change_data = {
            "thumbnail": serializer.validated_data.pop("thumbnail_url", ""),
            "dataset_names_before_changes": [lyr.alternate for lyr in instance.datasets],
        }

        instance = serializer.save()

        # thumbnail, events and resouce routines
        self._post_change_routines(
            instance=instance,
            create_action_perfomed=False,
            additional_data=post_change_data,
        )

    def _post_change_routines(self, instance: Map, create_action_perfomed: bool, additional_data: dict):
        # Invalidate cached API responses for this map. Defer to
        # transaction.on_commit so the cache version is bumped only
        # after the DB write is visible to other connections — without
        # this, a concurrent GET between the bump and the commit would
        # read the OLD blob at READ COMMITTED, cache it at the NEW
        # version key, and serve it stale for MAP_RETRIEVE_CACHE_TTL.
        pk = instance.pk
        transaction.on_commit(lambda: self.invalidate_map_cache(pk))
        # Step 1: Handle Maplayers signals if this is and update action
        if not create_action_perfomed:
            dataset_names_before_changes = additional_data.pop("dataset_names_before_changes", [])
            dataset_names_after_changes = [lyr.alternate for lyr in instance.datasets]
            if dataset_names_before_changes != dataset_names_after_changes:
                map_changed_signal.send_robust(sender=instance, what_changed="datasets")
        # Step 2: Register Event
        event_type = EventType.EVENT_CREATE if create_action_perfomed else EventType.EVENT_CHANGE
        register_event(self.request, event_type, instance)
        # Step 3: Resource Manager
        resource_manager.update(instance.uuid, instance=instance, notify=True)
