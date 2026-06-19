"""Tests for the generic seed data sync utility and secrets file parser."""

from __future__ import annotations

import json


from droid_deploy.assistant_deployments.seed_sync import (
    _aggregate_hash,
    _manager_api,
    _record_hash,
    _sync_contacts,
    _sync_guidance,
    sync_seed_data,
)
from droid.guidance_manager.types.guidance import Guidance
from droid_deploy.assistant_deployments.secrets_file import load_secrets

# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------


class TestRecordHash:
    def test_deterministic(self):
        r = {"a": 1, "b": "hello"}
        assert _record_hash(r, set()) == _record_hash(r, set())

    def test_excludes_id_field(self):
        r1 = {"contact_id": 1, "name": "Alice"}
        r2 = {"contact_id": 99, "name": "Alice"}
        assert _record_hash(r1, {"contact_id"}) == _record_hash(r2, {"contact_id"})

    def test_different_records_different_hash(self):
        r1 = {"name": "Alice"}
        r2 = {"name": "Bob"}
        assert _record_hash(r1, set()) != _record_hash(r2, set())


class TestAggregateHash:
    def test_empty_returns_empty_string(self):
        assert _aggregate_hash([], lambda r: r["k"], set()) == ""

    def test_deterministic(self):
        records = [{"k": "a", "v": 1}, {"k": "b", "v": 2}]
        h1 = _aggregate_hash(records, lambda r: r["k"], set())
        h2 = _aggregate_hash(records, lambda r: r["k"], set())
        assert h1 == h2

    def test_order_independent(self):
        r1 = [{"k": "a", "v": 1}, {"k": "b", "v": 2}]
        r2 = [{"k": "b", "v": 2}, {"k": "a", "v": 1}]
        h1 = _aggregate_hash(r1, lambda r: r["k"], set())
        h2 = _aggregate_hash(r2, lambda r: r["k"], set())
        assert h1 == h2


class TestManagerApi:
    def test_prefers_public_method_by_default(self):
        class Manager:
            def _create_contact(self):
                return "private"

            def create_contact(self):
                return "public"

        method = _manager_api(Manager(), "create_contact")

        assert method() == "public"

    def test_falls_back_to_private_method(self):
        class Manager:
            def _create_contact(self):
                return "private"

        method = _manager_api(Manager(), "create_contact")

        assert method() == "private"

    def test_raises_helpful_error_when_missing(self):
        class Manager:
            pass

        try:
            _manager_api(Manager(), "create_contact")
        except AttributeError as exc:
            assert "Manager has none of the expected methods" in str(exc)
            assert "create_contact, _create_contact" in str(exc)
        else:
            raise AssertionError("_manager_api should raise for missing methods")


# ---------------------------------------------------------------------------
# sync_seed_data (in-memory mock)
# ---------------------------------------------------------------------------


class InMemoryMetaStore:
    """Mock meta store for testing without DB."""

    def __init__(self):
        self._hashes: dict[str, str] = {}

    def get_hash(self, key: str) -> str:
        return self._hashes.get(key, "")

    def set_hash(self, key: str, value: str) -> None:
        self._hashes[key] = value


class TestSyncSeedData:
    def test_creates_new_records(self):
        created = []
        meta = InMemoryMetaStore()

        result = sync_seed_data(
            manager_key="test",
            source_records=[{"name": "Alice"}, {"name": "Bob"}],
            natural_key_fn=lambda r: r["name"],
            get_existing_fn=lambda: [],
            create_fn=lambda r: created.append(r),
            update_fn=None,
            delete_fn=None,
            id_field="id",
            meta_store=meta,
        )

        assert result is True
        assert len(created) == 2
        assert meta.get_hash("test") != ""

    def test_skips_when_hash_matches(self):
        created = []
        meta = InMemoryMetaStore()

        source = [{"name": "Alice"}]
        sync_seed_data(
            manager_key="test",
            source_records=source,
            natural_key_fn=lambda r: r["name"],
            get_existing_fn=lambda: [],
            create_fn=lambda r: created.append(r),
            update_fn=None,
            delete_fn=None,
            id_field="id",
            meta_store=meta,
        )
        created.clear()

        result = sync_seed_data(
            manager_key="test",
            source_records=source,
            natural_key_fn=lambda r: r["name"],
            get_existing_fn=lambda: [{"id": 1, "name": "Alice"}],
            create_fn=lambda r: created.append(r),
            update_fn=None,
            delete_fn=None,
            id_field="id",
            meta_store=meta,
        )

        assert result is False
        assert len(created) == 0

    def test_updates_changed_records(self):
        updated = []
        meta = InMemoryMetaStore()

        existing = [{"id": 1, "name": "Alice", "bio": "Old bio"}]

        result = sync_seed_data(
            manager_key="test",
            source_records=[{"name": "Alice", "bio": "New bio"}],
            natural_key_fn=lambda r: r["name"],
            get_existing_fn=lambda: existing,
            create_fn=lambda r: None,
            update_fn=lambda _id, r: updated.append((_id, r)),
            delete_fn=None,
            id_field="id",
            meta_store=meta,
        )

        assert result is True
        assert len(updated) == 1
        assert updated[0][0] == 1

    def test_excludes_backend_managed_fields_from_diff(self):
        updated = []
        meta = InMemoryMetaStore()

        result = sync_seed_data(
            manager_key="test",
            source_records=[
                {"name": "Alice", "bio": "Current bio", "authoring_assistant_id": None},
            ],
            natural_key_fn=lambda r: r["name"],
            get_existing_fn=lambda: [
                {
                    "id": 1,
                    "name": "Alice",
                    "bio": "Current bio",
                    "authoring_assistant_id": 1851,
                },
            ],
            create_fn=lambda r: None,
            update_fn=lambda _id, r: updated.append((_id, r)),
            delete_fn=None,
            id_field="id",
            meta_store=meta,
            exclude_fields={"authoring_assistant_id"},
        )

        assert result is True
        assert updated == []

    def test_guidance_sync_does_not_write_readonly_fields(self, monkeypatch):
        class FakeGuidanceManager:
            def __init__(self):
                self.created = []

            def filter(self, limit=1000):
                return []

            def add_guidance(self, *, title, content, images, function_ids):
                self.created.append(
                    {
                        "title": title,
                        "content": content,
                        "images": images,
                        "function_ids": function_ids,
                    },
                )
                return {"details": {"guidance_id": 1}}

            def update_guidance(self, **kwargs):
                raise AssertionError("update_guidance should not be called")

            def delete_guidance(self, **kwargs):
                raise AssertionError("delete_guidance should not be called")

        fake = FakeGuidanceManager()
        from droid.manager_registry import ManagerRegistry

        monkeypatch.setattr(
            ManagerRegistry,
            "get_guidance_manager",
            staticmethod(lambda: fake),
        )

        result = _sync_guidance(
            [
                Guidance(
                    title="How to triage repairs",
                    content="Check urgent repairs first.",
                    authoring_assistant_id=1851,
                ),
            ],
            InMemoryMetaStore(),
        )

        assert result is True
        assert len(fake.created) == 1
        assert "guidance_id" not in fake.created[0]
        assert "authoring_assistant_id" not in fake.created[0]
        assert "is_builtin" not in fake.created[0]

    def test_guidance_sync_ignores_existing_builtins(self, monkeypatch):
        class ExistingGuidance:
            def __init__(self, **kwargs):
                self._kwargs = kwargs

            def model_dump(self):
                return dict(self._kwargs)

        class FakeGuidanceManager:
            def __init__(self):
                self.created = []

            def filter(self, limit=1000):
                return [
                    ExistingGuidance(
                        guidance_id=500,
                        title="Builtin runbook",
                        content="Platform content.",
                        images=[],
                        function_ids=[],
                        is_builtin=True,
                    ),
                ]

            def add_guidance(self, *, title, content, images, function_ids):
                self.created.append(
                    {
                        "title": title,
                        "content": content,
                        "images": images,
                        "function_ids": function_ids,
                    },
                )
                return {"details": {"guidance_id": 1}}

            def update_guidance(self, **kwargs):
                raise AssertionError("update_guidance should not be called")

            def delete_guidance(self, **kwargs):
                raise AssertionError("builtins must not be deleted")

        fake = FakeGuidanceManager()
        from droid.manager_registry import ManagerRegistry

        monkeypatch.setattr(
            ManagerRegistry,
            "get_guidance_manager",
            staticmethod(lambda: fake),
        )

        result = _sync_guidance(
            [
                Guidance(
                    title="Tenant runbook",
                    content="Deployment-defined content.",
                ),
            ],
            InMemoryMetaStore(),
        )

        assert result is True
        assert [created["title"] for created in fake.created] == ["Tenant runbook"]

    def test_contact_sync_uses_contact_manager_create_api(self, monkeypatch):
        class FakeContactManager:
            def __init__(self):
                self.created = []

            def filter_contacts(self, limit=1000):
                return {"contacts": []}

            def _create_contact(self, **kwargs):
                self.created.append(kwargs)
                return {"details": {"contact_id": 1}}

            def update_contact(self, **kwargs):
                raise AssertionError("update_contact should not be called")

        fake = FakeContactManager()
        from droid.manager_registry import ManagerRegistry

        monkeypatch.setattr(
            ManagerRegistry,
            "get_contact_manager",
            staticmethod(lambda: fake),
        )

        result = _sync_contacts(
            [
                {
                    "contact_id": 99,
                    "first_name": "Seed",
                    "surname": "Contact",
                    "email_address": "seed@example.com",
                },
            ],
            InMemoryMetaStore(),
        )

        assert result is True
        assert fake.created == [
            {
                "first_name": "Seed",
                "surname": "Contact",
                "email_address": "seed@example.com",
            },
        ]

    def test_deletes_removed_records(self):
        deleted = []
        meta = InMemoryMetaStore()

        existing = [
            {"id": 1, "name": "Alice"},
            {"id": 2, "name": "Bob"},
        ]

        result = sync_seed_data(
            manager_key="test",
            source_records=[{"name": "Alice"}],
            natural_key_fn=lambda r: r["name"],
            get_existing_fn=lambda: existing,
            create_fn=lambda r: None,
            update_fn=None,
            delete_fn=lambda _id: deleted.append(_id),
            id_field="id",
            meta_store=meta,
        )

        assert result is True
        assert deleted == [2]

    def test_no_delete_when_delete_fn_is_none(self):
        meta = InMemoryMetaStore()
        existing = [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]

        result = sync_seed_data(
            manager_key="test",
            source_records=[{"name": "Alice"}],
            natural_key_fn=lambda r: r["name"],
            get_existing_fn=lambda: existing,
            create_fn=lambda r: None,
            update_fn=None,
            delete_fn=None,
            id_field="id",
            meta_store=meta,
        )

        assert result is True

    def test_empty_source_is_fast_noop_when_hash_matches(self):
        meta = InMemoryMetaStore()
        meta.set_hash("test", "")

        result = sync_seed_data(
            manager_key="test",
            source_records=[],
            natural_key_fn=lambda r: "",
            get_existing_fn=lambda: [],
            create_fn=lambda r: None,
            update_fn=None,
            delete_fn=None,
            id_field="id",
            meta_store=meta,
        )

        assert result is False


# ---------------------------------------------------------------------------
# Secrets file parser
# ---------------------------------------------------------------------------


class TestLoadSecrets:
    def test_returns_empty_when_file_missing(self, tmp_path):
        result = load_secrets(org_id=1, path=tmp_path / "nonexistent.json")
        assert result == []

    def test_parses_org_secrets(self, tmp_path):
        f = tmp_path / ".secrets.json"
        f.write_text(
            json.dumps(
                {
                    "org": {
                        "42": {
                            "API_KEY": {"value": "secret123", "description": "API key"},
                        },
                    },
                    "user": {},
                    "assistant": {},
                },
            ),
        )
        result = load_secrets(org_id=42, path=f)
        assert len(result) == 1
        assert result[0]["name"] == "API_KEY"
        assert result[0]["value"] == "secret123"

    def test_user_overrides_org(self, tmp_path):
        f = tmp_path / ".secrets.json"
        f.write_text(
            json.dumps(
                {
                    "org": {
                        "1": {
                            "KEY": {"value": "org-val", "description": "org"},
                        },
                    },
                    "user": {
                        "u1": {
                            "KEY": {"value": "user-val", "description": "user"},
                        },
                    },
                    "assistant": {},
                },
            ),
        )
        result = load_secrets(org_id=1, user_id="u1", path=f)
        assert len(result) == 1
        assert result[0]["value"] == "user-val"

    def test_secrets_merge_distinct_keys_across_levels(self, tmp_path):
        f = tmp_path / ".secrets.json"
        f.write_text(
            json.dumps(
                {
                    "org": {
                        "1": {
                            "ORG_KEY": {"value": "o", "description": "org key"},
                        },
                    },
                    "user": {
                        "u1": {
                            "USER_KEY": {"value": "u", "description": "user key"},
                        },
                    },
                    "assistant": {},
                },
            ),
        )
        result = load_secrets(org_id=1, user_id="u1", path=f)
        names = {s["name"] for s in result}
        assert names == {"ORG_KEY", "USER_KEY"}

    def test_no_matching_ids_returns_empty(self, tmp_path):
        f = tmp_path / ".secrets.json"
        f.write_text(
            json.dumps(
                {
                    "org": {"99": {"K": {"value": "v", "description": "d"}}},
                    "user": {},
                    "assistant": {},
                },
            ),
        )
        result = load_secrets(org_id=1, path=f)
        assert result == []

    def test_malformed_json_returns_empty(self, tmp_path):
        f = tmp_path / ".secrets.json"
        f.write_text("not valid json {{{")
        result = load_secrets(org_id=1, path=f)
        assert result == []
