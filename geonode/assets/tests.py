#########################################################################
#
# Copyright (C) 2024 OSGeo
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

import os
import logging
import shutil
import io
import json
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import StreamingHttpResponse
from django.urls import reverse


from rest_framework.test import APITestCase
from geonode.tests.base import GeoNodeBaseTestSupport

from geonode.assets.handlers import asset_handler_registry
from geonode.assets.local import LocalAssetHandler
from geonode.assets.models import Asset, LocalAsset
from geonode.assets.utils import create_asset, create_asset_and_link, unlink_asset
from geonode.base.models import ResourceBase, Link
from geonode.security.registry import permissions_registry

logger = logging.getLogger(__name__)

ONE_JSON = os.path.join(os.path.dirname(__file__), "tests/data/one.json")
TWO_JSON = os.path.join(os.path.dirname(__file__), "tests/data/two.json")
THREE_JSON = os.path.join(os.path.dirname(__file__), "tests/data/three.json")


class AssetsTests(APITestCase):

    def test_handler_registry(self):
        # Test registry
        self.assertIsNotNone(asset_handler_registry)
        # Test default handler
        asset_handler = asset_handler_registry.get_default_handler()
        self.assertIsNotNone(asset_handler)
        self.assertIsInstance(asset_handler, LocalAssetHandler, "Bad default Asset handler found")
        # Test None
        self.assertIsNone(asset_handler_registry.get_handler(None))
        # Test class without handler
        self.assertIsNone(asset_handler_registry.get_handler(AssetsTests))

    def test_creation_and_delete_data_cloned(self):
        u, _ = get_user_model().objects.get_or_create(username="admin")
        assets_root = os.path.normpath(settings.ASSETS_ROOT)

        asset_handler = asset_handler_registry.get_default_handler()
        asset = asset_handler.create(
            title="Test Asset",
            description="Description of test asset",
            type="NeverMind",
            owner=u,
            files=[ONE_JSON],
            clone_files=True,
        )
        asset.save()
        self.assertIsInstance(asset, LocalAsset)

        reloaded = LocalAsset.objects.get(pk=asset.pk)
        self.assertIsNotNone(reloaded)
        self.assertIsInstance(reloaded, LocalAsset)
        file = reloaded.location[0]
        self.assertTrue(os.path.exists(file), "Asset file does not exist")
        self.assertTrue(
            os.path.normpath(file).startswith(os.path.normpath(assets_root)),
            f"Asset file is not inside the assets root: {file}",
        )

        cloned_file = file
        # Hydrata divergence (TASK-1572): asset-dir removal is deferred to
        # transaction.on_commit; under the test's outer transaction it must be
        # executed explicitly to assert the post-commit filesystem state.
        with self.captureOnCommitCallbacks(execute=True):
            reloaded.delete()
        self.assertFalse(Asset.objects.filter(pk=asset.pk).exists())
        self.assertFalse(os.path.exists(cloned_file))
        self.assertFalse(os.path.exists(os.path.dirname(cloned_file)))
        self.assertTrue(os.path.exists(ONE_JSON))

    def test_creation_and_delete_data_external(self):
        u, _ = get_user_model().objects.get_or_create(username="admin")

        asset_handler = asset_handler_registry.get_default_handler()
        asset = asset_handler.create(
            title="Test Asset",
            description="Description of test asset",
            type="NeverMind",
            owner=u,
            files=[ONE_JSON],
            clone_files=False,
        )
        asset.save()
        self.assertIsInstance(asset, LocalAsset)

        reloaded = LocalAsset.objects.get(pk=asset.pk)
        self.assertIsNotNone(reloaded)
        self.assertIsInstance(reloaded, LocalAsset)
        file = reloaded.location[0]
        self.assertEqual(ONE_JSON, file)

        reloaded.delete()
        self.assertFalse(Asset.objects.filter(pk=asset.pk).exists())
        self.assertTrue(os.path.exists(ONE_JSON))

    def test_clone_and_delete_data_managed(self):
        u, _ = get_user_model().objects.get_or_create(username="admin")

        asset_handler = asset_handler_registry.get_default_handler()
        asset = asset_handler.create(
            title="Test Asset",
            description="Description of test asset",
            type="NeverMind",
            owner=u,
            files=[ONE_JSON],
            clone_files=True,
        )
        asset.save()
        self.assertIsInstance(asset, LocalAsset)

        reloaded = LocalAsset.objects.get(pk=asset.pk)
        cloned = asset_handler.clone(reloaded)
        self.assertNotEqual(reloaded.pk, cloned.pk)

        reloaded_file = os.path.normpath(reloaded.location[0])
        cloned_file = os.path.normpath(cloned.location[0])

        self.assertNotEqual(reloaded_file, cloned_file)
        self.assertTrue(os.path.exists(reloaded_file))
        self.assertTrue(os.path.exists(cloned_file))

        # Hydrata divergence (TASK-1572): on_commit-deferred asset-dir removal.
        with self.captureOnCommitCallbacks(execute=True):
            reloaded.delete()
        self.assertFalse(os.path.exists(reloaded_file))
        self.assertTrue(os.path.exists(cloned_file))

        with self.captureOnCommitCallbacks(execute=True):
            cloned.delete()
        self.assertFalse(os.path.exists(cloned_file))

    def test_clone_and_delete_data_unmanaged(self):
        u, _ = get_user_model().objects.get_or_create(username="admin")

        asset_handler = asset_handler_registry.get_default_handler()
        asset = asset_handler.create(
            title="Test Asset",
            description="Description of test asset",
            type="NeverMind",
            owner=u,
            files=[ONE_JSON],
            clone_files=False,
        )
        asset.save()
        self.assertIsInstance(asset, LocalAsset)

        reloaded = LocalAsset.objects.get(pk=asset.pk)
        cloned = asset_handler.clone(reloaded)

        self.assertEqual(reloaded.location[0], cloned.location[0])
        self.assertTrue(os.path.exists(reloaded.location[0]))

        reloaded.delete()
        self.assertTrue(os.path.exists(reloaded.location[0]))

        cloned.delete()
        self.assertTrue(os.path.exists(reloaded.location[0]))

    def test_clone_mixed_data(self):
        u, _ = get_user_model().objects.get_or_create(username="admin")

        asset_handler = asset_handler_registry.get_default_handler()
        managed_asset = asset_handler.create(
            title="Test Asset",
            description="Description of test asset",
            type="NeverMind",
            owner=u,
            files=[ONE_JSON],
            clone_files=True,
        )
        managed_asset.save()

        # TODO: dunno if mixed files should be allowed at all
        mixed_asset = asset_handler.create(
            title="Mixed Asset",
            description="Description of test asset",
            type="NeverMind",
            owner=u,
            files=[THREE_JSON, managed_asset.location[0]],
            clone_files=False,  # let's keep both managed and unmanaged together
        )

        reloaded = Asset.objects.get(pk=mixed_asset.pk)

        try:
            asset_handler.clone(reloaded)
            self.fail("A mixed LocalAsset has been cloned")
        except ValueError:
            pass

        # Hydrata divergence (TASK-1572): the post_delete cleanup (and any
        # ValueError it raises) is deferred to transaction.on_commit, so the
        # callbacks must be executed explicitly to observe the managed-dir
        # removal and the mixed-asset detection error.
        with self.captureOnCommitCallbacks(execute=True):
            managed_asset.delete()

        try:
            with self.captureOnCommitCallbacks(execute=True):
                mixed_asset.delete()
            self.fail("Missed mixed LocalAsset detection")
        except ValueError:
            pass

    def test_creation_with_multiple_in_memory_files(self):
        u, _ = get_user_model().objects.get_or_create(username="admin")
        asset_handler = asset_handler_registry.get_default_handler()

        dummy_content_1 = b"this is dummy file content 1"
        in_memory_file_1 = SimpleUploadedFile("test_file_1.txt", dummy_content_1, content_type="text/plain")

        dummy_content_2 = b"this is dummy file content 2"
        in_memory_file_2 = SimpleUploadedFile("test_file_2.txt", dummy_content_2, content_type="text/plain")

        asset = asset_handler.create(
            title="Test In-Memory Asset",
            description="Description of test in-memory asset",
            type="NeverMind",
            owner=u,
            files=[in_memory_file_1, in_memory_file_2],
        )
        self.assertIsInstance(asset, LocalAsset)

        reloaded = LocalAsset.objects.get(pk=asset.pk)
        self.assertEqual(len(reloaded.location), 2)

    def test_creation_with_multiple_cloned_local_files(self):
        u, _ = get_user_model().objects.get_or_create(username="admin")
        asset_handler = asset_handler_registry.get_default_handler()
        assets_root = os.path.normpath(settings.ASSETS_ROOT)

        local_files = [ONE_JSON, TWO_JSON]

        asset = asset_handler.create(
            title="Test Multiple Cloned Asset",
            description="Description of test multiple cloned asset",
            type="NeverMind",
            owner=u,
            files=local_files,
            clone_files=True,
        )
        self.assertIsInstance(asset, LocalAsset)

        reloaded = LocalAsset.objects.get(pk=asset.pk)
        self.assertEqual(len(reloaded.location), 2)

        for loc in reloaded.location:
            self.assertTrue(os.path.exists(loc))
            self.assertTrue(os.path.normpath(loc).startswith(assets_root))

        # Ensure original files are untouched
        self.assertTrue(os.path.exists(ONE_JSON))
        self.assertTrue(os.path.exists(TWO_JSON))

        parent_dir = os.path.dirname(reloaded.location[0])

        # Hydrata divergence (TASK-1572): on_commit-deferred asset-dir removal.
        with self.captureOnCommitCallbacks(execute=True):
            reloaded.delete()

        self.assertFalse(os.path.exists(parent_dir))
        # Ensure original files still exist after asset deletion
        self.assertTrue(os.path.exists(ONE_JSON))
        self.assertTrue(os.path.exists(TWO_JSON))

    def test_creation_with_mixed_cloned_and_in_memory_files(self):
        u, _ = get_user_model().objects.get_or_create(username="admin")
        asset_handler = asset_handler_registry.get_default_handler()
        assets_root = os.path.normpath(settings.ASSETS_ROOT)

        dummy_content = b"this is a mixed dummy file"
        in_memory_file = SimpleUploadedFile("mixed_test.txt", dummy_content, content_type="text/plain")

        files_to_create = [ONE_JSON, in_memory_file]

        asset = asset_handler.create(
            title="Test Mixed Asset",
            description="Description of test mixed asset",
            type="NeverMind",
            owner=u,
            files=files_to_create,
            clone_files=True,
        )
        self.assertIsInstance(asset, LocalAsset)

        reloaded = LocalAsset.objects.get(pk=asset.pk)
        self.assertEqual(len(reloaded.location), 2)

        for loc in reloaded.location:
            self.assertTrue(os.path.exists(loc))
            self.assertTrue(os.path.normpath(loc).startswith(assets_root))

        in_memory_path = next((loc for loc in reloaded.location if "mixed_test.txt" in loc), None)
        self.assertIsNotNone(in_memory_path)
        with open(in_memory_path, "rb") as f:
            content = f.read()
        self.assertEqual(content, dummy_content)

        reloaded.delete()
        self.assertTrue(os.path.exists(ONE_JSON))


class AssetsDownloadTests(APITestCase):

    fixtures = ["initial_data.json", "group_test_data.json", "default_oauth_apps.json"]

    def _get_streaming_content(self, response):
        with io.BytesIO(b"".join(response.streaming_content)) as buf_bytes:
            return buf_bytes.read()

    def test_download_file(self):
        u, _ = get_user_model().objects.get_or_create(username="admin")
        self.assertTrue(self.client.login(username="admin", password="admin"), "Login failed")

        asset_handler = asset_handler_registry.get_default_handler()
        asset = asset_handler.create(
            title="Test Asset",
            description="Description of test asset",
            type="NeverMind",
            owner=u,
            files=[ONE_JSON],
            clone_files=True,
        )
        asset.save()
        self.assertIsInstance(asset, LocalAsset)

        reloaded = LocalAsset.objects.get(pk=asset.pk)

        # put two more files in the asset dir
        asset_dir = os.path.dirname(reloaded.location[0])
        sub_dir = os.path.join(asset_dir, "subdir")
        os.mkdir(sub_dir)
        shutil.copy(TWO_JSON, asset_dir)
        shutil.copy(THREE_JSON, sub_dir)

        for path, key in ((None, "one"), ("one.json", "one"), ("two.json", "two"), ("subdir/three.json", "three")):
            # args = [asset.pk, path] if path else [asset.pk]
            args = {"pk": asset.pk, "path": path} if path else {"pk": asset.pk}
            logger.info(f"*** Testing path '{path}' args {args}")
            url = reverse("assets-link", kwargs=args)
            logger.info(f"REVERSE url is {url}")
            response = self.client.get(url)
            content = self._get_streaming_content(response)
            rjson = json.loads(content)
            self.assertEqual(response.status_code, 200)
            self.assertIn(key, rjson, f"Key '{key}' not found in path '{path}': {rjson} URL {url}")
            logger.info(f"Test for path '{path}' OK")

    def test_download_with_attachment(self):
        u, _ = get_user_model().objects.get_or_create(username="admin")
        self.assertTrue(self.client.login(username="admin", password="admin"), "Login failed")

        for key, el in (("one", ONE_JSON), ("two", TWO_JSON), ("three", THREE_JSON)):
            asset = self._setup_test(u, _file=el)

            url = reverse("assets-download", kwargs={"pk": asset.pk})
            logger.info(f"REVERSE url is {url}")
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(isinstance(response, StreamingHttpResponse))
            self.assertEqual(response.get("Content-Disposition"), f"attachment; filename={key}.zip")

    def test_download_with_attachment_for_anonymous(self):
        """
        any user without download permissions
        cannot download the asset
        """
        from geonode.resource.manager import resource_manager
        from geonode.layers.models import Dataset
        from guardian.shortcuts import get_anonymous_user

        bobby, _ = get_user_model().objects.get_or_create(username="bobby")

        self.client.force_login(get_user_model().objects.get(username="norman"))
        for _, el in (("one", ONE_JSON), ("two", TWO_JSON), ("three", THREE_JSON)):
            asset = self._setup_test(bobby, _file=el)
            resource = None
            try:
                # creating resoruce
                resource = resource_manager.create(
                    None, resource_type=Dataset, defaults={"owner": bobby, "asset": asset}
                )
                # getting perms
                resource_perms = permissions_registry.get_perms(instance=resource)
                # remove perms for user bobby
                resource_perms["users"][bobby] = ["view_resourcebase"]
                # and anonymous just to be sure
                resource_perms["groups"][get_anonymous_user().groups.first()] = ["view_resourcebase"]
                # resetting the permissions for the resource
                resource_manager.set_permissions(resource.uuid, instance=resource, permissions=resource_perms)
                url = reverse("assets-download", kwargs={"pk": asset.pk})
                response = self.client.get(url)
                self.assertEqual(response.status_code, 403)
            except Exception as e:
                logger.exception(e)
            finally:
                if resource:
                    resource.delete()

    def _setup_test(self, u, _file=ONE_JSON):
        asset_handler = asset_handler_registry.get_default_handler()
        asset = asset_handler.create(
            title="Test Asset",
            description="Description of test asset",
            type="NeverMind",
            owner=u,
            files=[_file],
            clone_files=True,
        )
        asset.save()
        self.assertIsInstance(asset, LocalAsset)

        reloaded = LocalAsset.objects.get(pk=asset.pk)

        # put two more files in the asset dir
        asset_dir = os.path.dirname(reloaded.location[0])
        sub_dir = os.path.join(asset_dir, "subdir")
        os.mkdir(sub_dir)
        shutil.copy(TWO_JSON, asset_dir)
        shutil.copy(THREE_JSON, sub_dir)
        return asset


class AssetCreationTests(GeoNodeBaseTestSupport):

    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.get(username="admin")
        self.created_files = []

    def tearDown(self):
        for file_path in self.created_files:
            if os.path.exists(file_path):
                os.remove(file_path)
        super().tearDown()

    def _create_dummy_file(self, filename="dummy.txt", content=b"dummy content"):
        """Create a real file on the filesystem and return its path."""
        os.makedirs(settings.MEDIA_ROOT, exist_ok=True)
        file_path = os.path.join(settings.MEDIA_ROOT, filename)
        with open(file_path, "wb") as f:
            f.write(content)
        self.created_files.append(file_path)
        return file_path

    def test_create_asset_function(self):
        """Test the create_asset utility function."""
        initial_asset_count = LocalAsset.objects.count()
        file_path = self._create_dummy_file(filename="test_create_asset.txt")
        asset = create_asset(
            self.user,
            [file_path],
            asset_type="document",
            title="Test Asset from Function",
            description="Description for asset from function",
        )

        self.assertIsNotNone(asset)
        self.assertEqual(LocalAsset.objects.count(), initial_asset_count + 1)
        self.assertEqual(asset.title, "Test Asset from Function")
        self.assertEqual(asset.owner, self.user)
        asset_file_path = asset.localasset.location[0]
        self.assertTrue(os.path.exists(asset_file_path), f"File should exist at asset location: {asset_file_path}")

    def test_create_asset_and_link_function(self):
        """Test the create_asset_and_link utility function."""
        initial_asset_count = LocalAsset.objects.count()
        resource = ResourceBase.objects.create(
            title="Test Resource for Asset Link",
            owner=self.user,
            abstract="Abstract for linked resource",
            uuid=str(uuid4()),
        )

        file_path = self._create_dummy_file(filename="test_create_asset_link.txt")

        asset, link = create_asset_and_link(
            resource,
            self.user,
            [file_path],
            asset_type="image",
            title="Linked Asset from Function",
            description="Description for linked asset from function",
        )

        self.assertTrue(link)
        self.assertIsNotNone(asset)
        self.assertEqual(LocalAsset.objects.count(), initial_asset_count + 1)
        self.assertEqual(link.asset.title, "Linked Asset from Function")
        self.assertEqual(link.asset.owner, self.user)
        self.assertEqual(link.resource, resource)
        asset_file_path = asset.localasset.location[0]
        self.assertTrue(os.path.exists(asset_file_path), f"File should exist at asset location: {asset_file_path}")


class DeleteAssetTests(GeoNodeBaseTestSupport):

    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.get(username="admin")
        self.resource1 = ResourceBase.objects.create(
            title="Test Resource 1",
            owner=self.user,
            uuid=str(uuid4()),
        )
        self.resource2 = ResourceBase.objects.create(
            title="Test Resource 2",
            owner=self.user,
            uuid=str(uuid4()),
        )
        self.asset = create_asset(self.user, asset_type="document", title="Test Asset for Deletion", files=[ONE_JSON])
        self.protected_asset = create_asset(self.user, asset_type="document", title="Original", files=[TWO_JSON])
        self.link1 = Link.objects.create(resource=self.resource1, asset=self.asset)
        self.protected_link = Link.objects.create(resource=self.resource1, asset=self.protected_asset)

    def test_delete_protected_asset(self):
        """Test that a protected asset (e.g., 'Original') is not deleted."""
        self.assertTrue(Asset.objects.filter(pk=self.protected_asset.pk).exists())
        self.assertTrue(Link.objects.filter(pk=self.protected_link.pk).exists())

        deleted, msg = unlink_asset(self.resource1, self.protected_asset)

        self.assertFalse(deleted)
        self.assertEqual(msg, "Asset is protected and will not be unlinked or deleted.")
        self.assertTrue(Asset.objects.filter(pk=self.protected_asset.pk).exists())
        self.assertTrue(Link.objects.filter(pk=self.protected_link.pk).exists())

    def test_delete_asset_no_link(self):
        """Test that nothing happens if there is no link between the resource and the asset."""
        asset_no_link = create_asset(self.user, title="Asset with no link", files=[THREE_JSON])
        self.assertTrue(Asset.objects.filter(pk=asset_no_link.pk).exists())

        deleted, msg = unlink_asset(self.resource1, asset_no_link)

        self.assertFalse(deleted)
        self.assertEqual(msg, "Link between resource and asset not found.")
        self.assertTrue(Asset.objects.filter(pk=asset_no_link.pk).exists())

    def test_delete_asset_linked_to_other_resources(self):
        """Test that only the link is deleted when the asset is linked to other resources."""
        # Link the asset to a second resource
        link2 = Link.objects.create(resource=self.resource2, asset=self.asset)
        self.assertEqual(Link.objects.filter(asset=self.asset).count(), 2)
        self.assertTrue(Asset.objects.filter(pk=self.asset.pk).exists())
        asset_pk = self.asset.pk
        other_links_count = Link.objects.filter(asset=self.asset).exclude(pk=link2.pk).count()
        deleted, msg = unlink_asset(self.resource1, self.asset)

        self.assertTrue(deleted)
        self.assertIn(
            f"Asset {asset_pk} was unlinked but not deleted, because still linked to {other_links_count} resources", msg
        )
        self.assertTrue(Asset.objects.filter(pk=self.asset.pk).exists())
        self.assertFalse(Link.objects.filter(pk=self.link1.pk).exists())
        self.assertTrue(Link.objects.filter(pk=link2.pk).exists())
        self.assertEqual(Link.objects.filter(asset=self.asset).count(), 1)

    def test_delete_asset_and_link(self):
        """Test that both the asset and the link are deleted when the asset is only linked to one resource."""
        self.assertEqual(Link.objects.filter(asset=self.asset).count(), 1)
        self.assertTrue(Asset.objects.filter(pk=self.asset.pk).exists())
        asset_pk = self.asset.pk
        asset_file_path = self.asset.localasset.location[0]
        self.assertTrue(os.path.exists(asset_file_path))

        # Hydrata divergence (TASK-1572): unlink_asset deletes the asset, whose
        # on_commit-deferred cleanup must be executed to assert the file is gone.
        with self.captureOnCommitCallbacks(execute=True):
            deleted, msg = unlink_asset(self.resource1, self.asset)

        self.assertTrue(deleted)
        self.assertIn(f"Asset {asset_pk} was unlinked and deleted from resource {self.resource1.pk}.", msg)
        self.assertFalse(Asset.objects.filter(pk=asset_pk).exists())
        self.assertFalse(Link.objects.filter(pk=self.link1.pk).exists())
        self.assertFalse(os.path.exists(asset_file_path))


# ---------------------------------------------------------------------------
# Hydrata divergence (TASK-1570): asset-dir orphan-corruption hardening.
# F1 (TASK-1571): remove_data tolerates an already-missing managed dir.
# F2 (TASK-1572): cleanup_asset_data defers the filesystem removal to
#                 transaction.on_commit so a rolled-back delete never touches
#                 disk.
# ---------------------------------------------------------------------------
class HydrataAssetHardeningTests(APITestCase):
    """Regression tests for the orphan-dir corruption hardening (TASK-1570)."""

    def _make_managed_asset(self, username="hydrata_admin"):
        u, _ = get_user_model().objects.get_or_create(username=username)
        handler = asset_handler_registry.get_default_handler()
        asset = handler.create(
            title="Hydrata Test Asset",
            description="orphan-dir hardening fixture",
            type="NeverMind",
            owner=u,
            files=[ONE_JSON],
            clone_files=True,
        )
        asset.save()
        return handler, LocalAsset.objects.get(pk=asset.pk)

    # --- F1 (TASK-1571) -----------------------------------------------------
    def test_remove_data_tolerates_already_missing_dir(self):
        """remove_data on an asset whose managed dir is already gone SUCCEEDS."""
        handler, asset = self._make_managed_asset()
        managed_dir = handler._get_managed_dir(asset)
        self.assertTrue(os.path.isdir(managed_dir))

        # Simulate the corruption: the dir vanished out-of-band (e.g. a prior
        # rolled-back delete or terrain-reprocess that rmtree'd it).
        shutil.rmtree(managed_dir)
        self.assertFalse(os.path.exists(managed_dir))

        # Must NOT raise — "already gone" is success.
        handler.remove_data(asset)

    def test_delete_asset_with_missing_dir_succeeds(self):
        """Deleting a LocalAsset whose managed dir is gone does not 500."""
        handler, asset = self._make_managed_asset()
        managed_dir = handler._get_managed_dir(asset)
        shutil.rmtree(managed_dir)

        asset_pk = asset.pk
        asset.delete()  # post_delete -> cleanup_asset_data -> remove_data
        self.assertFalse(Asset.objects.filter(pk=asset_pk).exists())

    def test_remove_data_still_raises_on_mismatched_base(self):
        """An asset with mismatched managed base dirs STILL raises (not skipped)."""
        handler, asset = self._make_managed_asset()
        assets_root = os.path.normpath(settings.ASSETS_ROOT)
        # Two managed files in DIFFERENT managed subdirs -> mismatch -> ValueError.
        asset.location = [
            os.path.join(assets_root, "dirA", "f.json"),
            os.path.join(assets_root, "dirB", "f.json"),
        ]
        asset.save()
        with self.assertRaises(ValueError):
            handler.remove_data(asset)

    def test_get_managed_dir_still_strict_on_missing_dir(self):
        """_get_managed_dir (used by clone) stays strict: raises on missing dir."""
        handler, asset = self._make_managed_asset()
        managed_dir = handler._get_managed_dir(asset)
        shutil.rmtree(managed_dir)
        with self.assertRaises(ValueError):
            handler._get_managed_dir(asset)

    # --- F2 (TASK-1572) -----------------------------------------------------
    def test_delete_rolled_back_leaves_dir_on_disk(self):
        """A rolled-back delete must NEVER touch the filesystem (W1 gate).

        The filesystem removal is deferred to transaction.on_commit, so when the
        surrounding transaction rolls back the rmtree is discarded and no
        orphaned dir is created (the corruption is never produced)."""
        from django.db import transaction

        handler, asset = self._make_managed_asset()
        managed_dir = handler._get_managed_dir(asset)
        self.assertTrue(os.path.isdir(managed_dir))

        class _DeliberateCascadeFailure(Exception):
            pass

        with self.assertRaises(_DeliberateCascadeFailure):
            with transaction.atomic():
                asset.delete()  # queues on_commit(remove_data)
                # Simulate a mid-cascade failure AFTER the asset row delete.
                raise _DeliberateCascadeFailure("boom")

        # Rollback: the on_commit callback never fired -> dir survives on disk.
        self.assertTrue(
            os.path.isdir(managed_dir),
            "Rolled-back delete must not remove the managed dir (no orphan created)",
        )

    def test_delete_committed_removes_dir(self):
        """On a successful (committed) delete the managed dir IS removed.

        Uses captureOnCommitCallbacks because the test itself runs inside an
        atomic block (APITestCase), so on_commit callbacks are otherwise held
        until the test's outer transaction — captureOnCommitCallbacks executes
        them deterministically to assert the deferred removal."""
        handler, asset = self._make_managed_asset()
        managed_dir = handler._get_managed_dir(asset)
        self.assertTrue(os.path.isdir(managed_dir))

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            asset.delete()
            # Removal is deferred: still on disk until the on_commit fires.
            self.assertTrue(os.path.isdir(managed_dir))

        # A removal callback was queued and ran on (simulated) commit.
        self.assertGreaterEqual(len(callbacks), 1)
        self.assertFalse(os.path.exists(managed_dir))
