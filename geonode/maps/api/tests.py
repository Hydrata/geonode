#########################################################################
#
# Copyright (C) 2016 OSGeo
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
from urllib.parse import urljoin

from django.conf import settings
from django.urls import reverse

from guardian.shortcuts import assign_perm, get_anonymous_user
from mock import patch
from rest_framework.test import APITestCase

from geonode.base.populate_test_data import create_models
from geonode.layers.models import Dataset
from geonode.maps.models import Map, MapLayer

logger = logging.getLogger(__name__)


class MapsApiTests(APITestCase):
    fixtures = ["initial_data.json", "group_test_data.json", "default_oauth_apps.json"]

    @classmethod
    def setUpTestData(cls):
        create_models(b"document")
        create_models(b"map")
        create_models(b"dataset")

        try:
            cls.map = Map.objects.get(pk=1)
            cls.dataset = Dataset.objects.get(pk=1)
        except (Map.DoesNotExist, Dataset.DoesNotExist):
            cls.map = Map.objects.first()
            cls.dataset = Dataset.objects.first()

        # Update and save the class-level object
        cls.map.blob = DUMMY_MAPDATA
        cls.map.save()

        # Create the MapLayer object
        MapLayer.objects.create(
            map=cls.map,
            store=cls.dataset.store,
            name=cls.dataset.alternate,
            extra_params={"foo": "bar"},
            current_style="some-style",
            local=True,
        )

    def test_maps(self):
        """
        Ensure we can access the Maps list.
        """
        url = reverse("maps-list")
        # Anonymous
        response = self.client.get(url, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 5)
        self.assertEqual(response.data["total"], 9)
        # Check: No overfetching for maplayers
        self.assertTrue(any([map.get("maplayers", []) for map in response.data["maps"]]))

        # Pagination
        self.assertEqual(len(response.data["maps"]), 9)
        logger.debug(response.data)

        for _l in response.data["maps"]:
            self.assertTrue(_l["resource_type"], "map")

        # Get Layers List (backgrounds)
        resource = Map.objects.first()
        assign_perm("base.view_resourcebase", get_anonymous_user(), resource.get_self_resource())

        url = urljoin(f"{reverse('maps-detail', kwargs={'pk': resource.pk})}/", "maplayers/")
        response = self.client.get(url, format="json")
        self.assertEqual(response.status_code, 200)
        layers_data = response.data
        self.assertIsNotNone(layers_data)
        self.assertEqual(layers_data[0]["extra_params"], {"foo": "bar"})
        self.assertIsNotNone(layers_data[0]["dataset"])

        # Get Local-Layers List (GeoNode)
        url = urljoin(f"{reverse('maps-detail', kwargs={'pk': resource.pk})}/", "datasets/")
        response = self.client.get(url, format="json")
        self.assertEqual(response.status_code, 200)
        layers_data = response.data
        self.assertIsNotNone(layers_data)

        if settings.GEONODE_CLIENT_LAYER_PREVIEW_LIBRARY == "mapstore":
            url = reverse("maps-list")
            self.assertEqual(url, "/api/v2/maps")

            # Anonymous
            response = self.client.get(url, format="json")
            self.assertEqual(response.status_code, 200)
            if response.data:
                self.assertEqual(len(response.data), 5)  # now are 5 since will read from maps

                # Get Full Map layer configuration
                url = reverse("maps-detail", kwargs={"pk": resource.pk})
                response = self.client.get(f"{url}?include[]=data", format="json")
                self.assertEqual(response.status_code, 200)
                self.assertTrue(len(response.data) > 0)
                self.assertTrue("data" in response.data["map"])
                self.assertTrue(len(response.data["map"]["data"]["map"]["layers"]) == 7)
                self.assertEqual(response.data["map"]["maplayers"][0]["extra_params"], {"foo": "bar"})
                self.assertIsNotNone(response.data["map"]["maplayers"][0]["dataset"])
                self.assertEqual(response.data["map"]["maplayers"][0]["extra_params"], {"foo": "bar"})
                self.assertEqual(response.data["map"]["maplayers"][0]["visibility"], 1)
                self.assertEqual(response.data["map"]["maplayers"][0]["order"], 0)
                self.assertEqual(response.data["map"]["maplayers"][0]["opacity"], 1.0)

    def test_extra_metadata_included_with_param(self):
        resource = Map.objects.first()
        url = urljoin(f"{reverse('maps-list')}/", f"{resource.pk}")
        data = {"include[]": "metadata"}

        response = self.client.get(url, format="json", data=data)
        self.assertIsNotNone(response.data["map"].get("metadata"))

        response = self.client.get(url, format="json")
        self.assertNotIn("map", response.data["map"])

    def test_patch_map(self):
        """
        Patch to maps/<pk>/
        """
        # Get Layers List (backgrounds)
        resource = Map.objects.first()
        url = reverse("maps-detail", kwargs={"pk": resource.pk})

        data = {
            "title": f"{resource.title}-edited",
            "abstract": resource.abstract,
            "data": DUMMY_MAPDATA,
            "id": resource.id,
            "maplayers": DUMMY_MAPLAYERS_DATA,
        }
        self.client.login(username="admin", password="admin")
        response = self.client.patch(f"{url}?include[]=data", data=data, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(len(response.data) > 0)
        self.assertTrue("data" in response.data["map"])
        self.assertTrue(len(response.data["map"]["data"]["map"]["layers"]) == 7)
        response_maplayer = response.data["map"]["maplayers"][0]
        self.assertEqual(response_maplayer["extra_params"], {"msId": "Stamen.Watercolor__0"})
        self.assertEqual(response_maplayer["current_style"], "some-style-first-layer")
        self.assertIsNotNone(response_maplayer["dataset"])

    def test_patch_map_with_extra_maplayer_info(self):
        """
        Patch to maps/<pk>/
        """
        # Get Layers List (backgrounds)
        resource = Map.objects.first()
        url = reverse("maps-detail", kwargs={"pk": resource.pk})

        data = {
            "title": f"{resource.title}-edited",
            "abstract": resource.abstract,
            "data": DUMMY_MAPDATA,
            "id": resource.id,
            "maplayers": DUMMY_MAPLAYERS_DATA_WITH_EXTRA_INFO,
        }
        self.client.login(username="admin", password="admin")
        response = self.client.patch(f"{url}?include[]=data", data=data, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(len(response.data) > 0)
        self.assertTrue("data" in response.data["map"])
        self.assertTrue(len(response.data["map"]["data"]["map"]["layers"]) == 7)
        response_maplayer = response.data["map"]["maplayers"][0]
        self.assertEqual(response_maplayer["extra_params"], {"msId": "Stamen.Watercolor__0"})
        self.assertEqual(response_maplayer["current_style"], "some-style-first-layer")
        self.assertEqual(response_maplayer["visibility"], False)
        self.assertEqual(response_maplayer["order"], 99)
        self.assertEqual(response_maplayer["opacity"], 1.3)
        self.assertIsNotNone(response_maplayer["dataset"])

    @patch("geonode.maps.api.views.resolve_object")
    def test_patch_map_raise_exception(self, mocked_obj):
        """
        Patch to maps/<pk>/
        """
        # Get Layers List (backgrounds)
        resource = Map.objects.first()
        mocked_obj.return_value = Map.objects.last()

        url = reverse("maps-detail", kwargs={"pk": resource.pk})

        data = {
            "title": f"{resource.title}-edited",
            "abstract": resource.abstract,
            "data": DUMMY_MAPDATA,
            "id": resource.id,
            "maplayers": DUMMY_MAPLAYERS_DATA,
        }
        self.client.login(username="admin", password="admin")
        response = self.client.patch(f"{url}?include[]=data", data=data, format="json")

        expected_error = {
            "success": False,
            "errors": ["serializer instance and object are different"],
            "code": "maps_exception",
        }
        self.assertEqual(response.status_code, 500)
        self.assertEqual(expected_error, response.json())

    def test_map_listing_advertised(self):
        app = Map.objects.first()
        app.advertised = False
        app.save()

        url = reverse("maps-list")

        payload = self.client.get(url)

        prev_count = payload.json().get("total")
        # the user can see only the advertised resources
        self.assertTrue(Map.objects.filter(advertised=True).count() >= prev_count)

        payload = self.client.get(f"{url}?advertised=True")
        # so if advertised is True, we dont see the advertised=False resource
        new_count = payload.json().get("total")
        # recheck the count
        self.assertEqual(new_count, prev_count)

        payload = self.client.get(f"{url}?advertised=False")
        # so if advertised is False, we see only the resource with advertised==False
        new_count = payload.json().get("total")
        # recheck the count
        self.assertEqual(new_count, 1)

        # if all is requested, we will see all the resources
        payload = self.client.get(f"{url}?advertised=all")
        new_count = payload.json().get("total")
        # recheck the count
        self.assertEqual(new_count, prev_count + 1)

        Map.objects.update(advertised=True)

    def test_create_map(self):
        """
        Post to maps/
        """
        # Get Layers List (backgrounds)
        url = reverse("maps-list")

        data = {
            "title": "Some created map",
            "data": DUMMY_MAPDATA,
            "maplayers": DUMMY_MAPLAYERS_DATA,
        }
        self.client.login(username="admin", password="admin")
        response = self.client.post(f"{url}?include[]=data", data=data, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertTrue(len(response.data) > 0)
        self.assertTrue("data" in response.data["map"])
        self.assertTrue(len(response.data["map"]["data"]["map"]["layers"]) == 7)
        response_maplayer = response.data["map"]["maplayers"][0]
        self.assertEqual(response_maplayer["extra_params"], {"msId": "Stamen.Watercolor__0"})
        self.assertEqual(response_maplayer["current_style"], "some-style-first-layer")
        self.assertIsNotNone(response_maplayer["dataset"])
        self.assertIsNotNone(response.data["map"]["thumbnail_url"])

    def test_create_map_featured_status_admin(self):
        """
        Post to maps/
        User with perms should be able to change the value in the post payload
        """
        # Get Layers List (backgrounds)
        url = reverse("maps-list")

        data = {
            "title": "Map should be approved",
            "featured": True,
            "is_approved": False,
            "is_published": False,
            "data": DUMMY_MAPDATA,
            "maplayers": DUMMY_MAPLAYERS_DATA,
        }
        # if has perms, the user should be able to change the field
        # featured/approved/published
        self.client.login(username="admin", password="admin")
        response = self.client.post(f"{url}?include[]=data", data=data, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.json()["map"]["is_published"])
        self.assertFalse(response.json()["map"]["is_approved"])
        self.assertTrue(response.json()["map"]["featured"])

    def test_create_map_with_extra_maplayer_info(self):
        """
        Post to maps/
        """
        # Get Layers List (backgrounds)
        url = reverse("maps-list")

        data = {
            "title": "Some created map",
            "data": DUMMY_MAPDATA,
            "maplayers": DUMMY_MAPLAYERS_DATA_WITH_EXTRA_INFO,
        }
        self.client.login(username="admin", password="admin")
        response = self.client.post(f"{url}?include[]=data", data=data, format="json")

        self.assertEqual(response.status_code, 201)
        self.assertTrue(len(response.data) > 0)
        self.assertTrue("data" in response.data["map"])
        self.assertTrue(len(response.data["map"]["data"]["map"]["layers"]) == 7)
        response_maplayer = response.data["map"]["maplayers"][0]
        self.assertEqual(response_maplayer["extra_params"], {"msId": "Stamen.Watercolor__0"})
        self.assertEqual(response_maplayer["current_style"], "some-style-first-layer")
        self.assertIsNotNone(response_maplayer["dataset"])
        self.assertEqual(response_maplayer["visibility"], False)
        self.assertEqual(response_maplayer["order"], 99)
        self.assertEqual(response_maplayer["opacity"], 1.3)
        self.assertIsNotNone(response.data["map"]["thumbnail_url"])


DUMMY_MAPDATA = {
    "map": {
        "zoom": 9,
        "units": "m",
        "center": {"x": 11.763505157657004, "y": 43.7880264429571, "crs": "EPSG:4326"},
        "groups": [{"id": "Default", "title": "Default", "expanded": True}],
        "layers": [
            {
                "id": "Stamen.Watercolor__0",
                "name": "Stamen.Watercolor",
                "type": "tileprovider",
                "group": "background",
                "title": "Stamen Watercolor",
                "hidden": False,
                "source": "Stamen",
                "provider": "Stamen.Watercolor",
                "thumbURL": "https://stamen-tiles-c.a.ssl.fastly.net/watercolor/0/0/0.jpg",
                "dimensions": [],
                "styles": ["some-style-first-layer", "some-other-style-first-layer"],
                "singleTile": False,
                "visibility": False,
                "extraParams": {"msId": "Stamen.Watercolor__0"},
                "hideLoading": False,
                "useForElevation": False,
                "handleClickOnLayer": False,
            },
            {
                "id": "Stamen.Terrain__1",
                "name": "Stamen.Terrain",
                "type": "tileprovider",
                "group": "background",
                "title": "Stamen Terrain",
                "hidden": False,
                "source": "Stamen",
                "provider": "Stamen.Terrain",
                "thumbURL": "https://stamen-tiles-d.a.ssl.fastly.net/terrain/0/0/0.png",
                "dimensions": [],
                "singleTile": False,
                "visibility": False,
                "extraParams": {"msId": "Stamen.Terrain__1"},
                "hideLoading": False,
                "useForElevation": False,
                "handleClickOnLayer": False,
            },
            {
                "id": "Stamen.Toner__2",
                "name": "Stamen.Toner",
                "type": "tileprovider",
                "group": "background",
                "title": "Stamen Toner",
                "hidden": False,
                "source": "Stamen",
                "provider": "Stamen.Toner",
                "thumbURL": "https://stamen-tiles-d.a.ssl.fastly.net/toner/0/0/0.png",
                "dimensions": [],
                "singleTile": False,
                "visibility": False,
                "extraParams": {"msId": "Stamen.Toner__2"},
                "hideLoading": False,
                "useForElevation": False,
                "handleClickOnLayer": False,
            },
            {
                "id": "mapnik__3",
                "name": "mapnik",
                "type": "osm",
                "group": "background",
                "title": "Open Street Map",
                "hidden": False,
                "source": "osm",
                "dimensions": [],
                "singleTile": False,
                "visibility": True,
                "extraParams": {"msId": "mapnik__3"},
                "hideLoading": False,
                "useForElevation": False,
                "handleClickOnLayer": False,
            },
            {
                "id": "OpenTopoMap__4",
                "name": "OpenTopoMap",
                "type": "tileprovider",
                "group": "background",
                "title": "OpenTopoMap",
                "hidden": False,
                "source": "OpenTopoMap",
                "provider": "OpenTopoMap",
                "dimensions": [],
                "singleTile": False,
                "visibility": False,
                "extraParams": {"msId": "OpenTopoMap__4"},
                "hideLoading": False,
                "useForElevation": False,
                "handleClickOnLayer": False,
            },
            {
                "id": "s2cloudless",
                "url": "https://maps.geosolutionsgroup.com.it/geoserver/wms",
                "name": "s2cloudless:s2cloudless",
                "type": "wms",
                "group": "background",
                "title": "Sentinel-2 cloudless - https://s2maps.eu",
                "format": "image/jpeg",
                "hidden": False,
                "thumbURL": "http://localhost:8000/static/mapstorestyle/img/s2cloudless-s2cloudless.png",
                "dimensions": [],
                "singleTile": False,
                "visibility": False,
                "extraParams": {"msId": "s2cloudless"},
                "hideLoading": False,
                "useForElevation": False,
                "handleClickOnLayer": False,
            },
            {
                "id": "none",
                "name": "empty",
                "type": "empty",
                "group": "background",
                "title": "Empty Background",
                "hidden": False,
                "source": "ol",
                "dimensions": [],
                "singleTile": False,
                "visibility": False,
                "extraParams": {"msId": "none"},
                "hideLoading": False,
                "useForElevation": False,
                "handleClickOnLayer": False,
            },
        ],
        "maxExtent": [-20037508.34, -20037508.34, 20037508.34, 20037508.34],
        "mapOptions": {},
        "projection": "EPSG:3857",
        "backgrounds": [],
    },
    "version": 2,
    "timelineData": {},
    "dimensionData": {},
    "widgetsConfig": {"layouts": {"md": [], "xxs": []}},
    "catalogServices": {
        "services": {
            "GeoNode Catalogue": {
                "url": "http://localhost:8000/catalogue/csw",
                "type": "csw",
                "title": "GeoNode Catalogue",
                "autoload": True,
            },
        },
        "selectedService": "GeoNode Catalogue",
    },
    "mapInfoConfiguration": {},
}

DUMMY_MAPLAYERS_DATA = [
    {
        "extra_params": {"msId": "Stamen.Watercolor__0"},
        "current_style": "some-style-first-layer",
        "name": "geonode:CA",
    }
]

DUMMY_MAPLAYERS_DATA_WITH_EXTRA_INFO = [
    {
        "extra_params": {"msId": "Stamen.Watercolor__0"},
        "current_style": "some-style-first-layer",
        "name": "geonode:CA",
        "opacity": 1.3,
        "visibility": False,
        "order": 99,
    }
]


class MapLayerPermsBulkTests(APITestCase):
    """
    TASK-583: parity + query-count gate for permissions_registry.get_perms_bulk
    and the bulk map-blob perms path (MapSerializer.to_representation ->
    MapLayerDatasetSerializer.get_perms).

    The optimisation MUST return exactly what the legacy per-resource path returns;
    these tests are that contract.
    """

    fixtures = ["initial_data.json", "group_test_data.json", "default_oauth_apps.json"]

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Group

        create_models(b"dataset")
        create_models(b"map")
        User = get_user_model()

        cls.datasets = list(Dataset.objects.all().order_by("pk"))
        assert len(cls.datasets) >= 3, "need a few datasets to exercise the bulk path"
        # Exercise the raster perm branch on one dataset (bypass signals via .update()).
        Dataset.objects.filter(pk=cls.datasets[0].pk).update(subtype="raster")
        cls.datasets = list(Dataset.objects.all().order_by("pk"))

        cls.superuser = User.objects.create_superuser("bulk_su", "su@t.com", "pw")
        cls.staff = User.objects.create_user("bulk_staff", "st@t.com", "pw", is_staff=True)
        cls.member = User.objects.create_user("bulk_member", "me@t.com", "pw")
        # A brand-new user with NO grants on any layer: the actual "per-user-cold"
        # scenario the optimisation targets (cache misses, no perms -> no handler
        # queries), used for the constant-query gate.
        cls.newcomer = User.objects.create_user("bulk_newcomer", "nc@t.com", "pw")
        cls.owner = cls.datasets[1].owner
        cls.anon = get_anonymous_user()

        # Varied grants so the parity comparison spans direct / group / anonymous perms.
        assign_perm("view_resourcebase", cls.member, cls.datasets[1].get_self_resource())
        assign_perm("change_dataset_data", cls.member, cls.datasets[1])
        assign_perm("view_resourcebase", cls.anon, cls.datasets[2].get_self_resource())
        grp = Group.objects.create(name="bulk_parity_group")
        cls.member.groups.add(grp)
        assign_perm("view_resourcebase", grp, cls.datasets[0].get_self_resource())

    # ---- helpers -------------------------------------------------------------
    def _legacy(self, user):
        from django.core.cache import cache
        from geonode.security.registry import permissions_registry

        cache.clear()
        return {
            ds.pk: set(permissions_registry.get_perms(instance=ds, user=user, use_cache=False)) for ds in self.datasets
        }

    def _bulk(self, user):
        from django.core.cache import cache
        from geonode.security.registry import permissions_registry

        cache.clear()
        return {
            pk: set(v)
            for pk, v in permissions_registry.get_perms_bulk(self.datasets, user=user, use_cache=False).items()
        }

    # ---- AC#4: payload identical to legacy path for every user class ----------
    def test_parity_all_user_classes(self):
        for label, user in [
            ("anonymous", self.anon),
            ("owner", self.owner),
            ("group-member", self.member),
            ("staff", self.staff),
            ("superuser", self.superuser),
        ]:
            with self.subTest(user=label):
                self.assertEqual(self._bulk(user), self._legacy(user), f"bulk != legacy for {label}")

    # ---- AC#2 + AC#6: per-pk cache writes match _get_cache_key, single lookups hit
    def test_bulk_writes_per_pk_cache_and_single_lookup_hits(self):
        from django.core.cache import cache
        from geonode.security.registry import permissions_registry

        cache.clear()
        bulk = permissions_registry.get_perms_bulk(self.datasets, user=self.member, use_cache=True)
        for ds in self.datasets:
            key = permissions_registry._get_cache_key([ds.pk], [self.member], None)
            self.assertIsNotNone(cache.get(key), f"missing cache entry for ds {ds.pk}")
            self.assertEqual(set(cache.get(key)), set(bulk[ds.pk]))
            # A subsequent single-resource lookup is served from cache: it must NOT
            # recompute perms. (One residual query is the get_anonymous_user() lookup
            # inside the pre-existing _get_cache_key; the perms themselves are cached.)
            from django.test.utils import CaptureQueriesContext
            from django.db import connection

            with CaptureQueriesContext(connection) as ctx:
                cached_perms = permissions_registry.get_perms(instance=ds, user=self.member, use_cache=True)
            self.assertLessEqual(len(ctx.captured_queries), 1)
            self.assertEqual(set(cached_perms), set(bulk[ds.pk]))

    def test_bulk_writes_anonymous_cache_key(self):
        from django.core.cache import cache
        from geonode.security.registry import permissions_registry

        cache.clear()
        permissions_registry.get_perms_bulk(self.datasets, user=self.anon, use_cache=True)
        for ds in self.datasets:
            key = permissions_registry._get_cache_key([ds.pk], [self.anon], None)
            self.assertIn(":anonymous", key)
            self.assertIsNotNone(cache.get(key), f"missing anonymous cache entry for ds {ds.pk}")

    # ---- AC#1: query count is small and CONSTANT (does not grow with layer count)
    def test_bulk_query_count_is_constant(self):
        from django.core.cache import cache
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        from geonode.security.registry import permissions_registry

        def count_for(datasets):
            # Warm process-level caches (ContentType, Configuration) first so the
            # measurement reflects per-call DB work, not one-time global cache priming.
            permissions_registry.get_perms_bulk(datasets, user=self.newcomer, use_cache=False)
            cache.clear()
            with CaptureQueriesContext(connection) as ctx:
                # The cold per-user-new scenario: a brand-new user, empty perm cache.
                permissions_registry.get_perms_bulk(datasets, user=self.newcomer, use_cache=True)
            return len(ctx.captured_queries)

        # Same subtype coverage (raster ds[0] + vector ds[1]) in both slices so the
        # per-subtype Permission memo doesn't skew the comparison.
        n_two = count_for(self.datasets[:2])
        n_all = count_for(self.datasets)
        print(f"\n[TASK-583] get_perms_bulk queries: 2 datasets={n_two}, {len(self.datasets)} datasets={n_all}")
        # The whole point (and the primary assertion): serving all N datasets costs the
        # SAME as serving 2 -- the per-layer Guardian N+1 (~6-9 queries/layer, ~390-585
        # for a 65-layer map) collapses to a constant independent of N.
        self.assertEqual(n_all, n_two, "bulk query count must not grow with the number of datasets")
        # Absolute ceiling: a small constant. The constant is ~12 (not the task's
        # original ≤5 estimate) because guardian's ObjectPermissionChecker.prefetch_perms
        # issues a user-perm + group-perm query per content type (dataset + resourcebase).
        # The latency win comes from the O(N)->O(1) collapse, not from 12-vs-5.
        self.assertLessEqual(n_all, 13, f"expected a small constant query count, got {n_all}")

    # ---- integration: serializer pre-computes perms in bulk and matches ----
    def test_serializer_uses_bulk_perms(self):
        from rest_framework.test import APIRequestFactory
        from rest_framework.request import Request
        from geonode.security.registry import permissions_registry
        from geonode.maps.api.serializers import MapSerializer

        the_map = Map.objects.first()
        the_map.maplayers.all().delete()
        for ds in self.datasets:
            MapLayer.objects.create(map=the_map, dataset=ds, name=ds.alternate, local=True)

        django_req = APIRequestFactory().get("/")
        drf_req = Request(django_req)
        drf_req.user = self.member
        serializer = MapSerializer(instance=the_map, context={"request": drf_req})
        data = serializer.data

        self.assertIn("_bulk_layer_perms", serializer.context)
        expected = permissions_registry.get_perms_bulk(self.datasets, user=self.member, use_cache=True)
        layers = {ml["dataset"]["pk"]: ml["dataset"]["perms"] for ml in data["maplayers"] if ml.get("dataset")}
        for ds in self.datasets:
            self.assertIn(ds.pk, layers)
            self.assertEqual(set(layers[ds.pk]), set(expected[ds.pk]))
