"""
Tests for wildcard TLS certificate distribution to VMs.

Verifies that create_windows_vm() and create_ubuntu_vm() fetch the
wildcard cert from Secret Manager and include it in VM metadata, and
that VM creation proceeds gracefully when the cert is not available.
"""

from unittest.mock import patch, MagicMock

_FAKE_CERT = "-----BEGIN CERTIFICATE-----\nMIIFake...\n-----END CERTIFICATE-----\n"
_FAKE_KEY = "-----BEGIN PRIVATE KEY-----\nMIIFake...\n-----END PRIVATE KEY-----\n"

_COMMON_ARGS = dict(
    assistant_id="999",
    static_ip="10.0.0.1",
    hostname="unity-assistant-999-staging.vm.unify.ai",
    unify_apikey="test-api-key",
    assistant_name="999",
)


def _extract_metadata_dict(instance_resource):
    """Pull the metadata items from a compute_v1.Instance into a dict."""
    return {item.key: item.value for item in instance_resource.metadata.items}


class TestWindowsVmWildcardCert:

    @patch("communication.infra.vm_helpers.get_secret")
    @patch("communication.infra.vm_helpers.compute_v1.InstancesClient")
    def test_includes_tls_metadata_when_cert_available(
        self,
        mock_client_cls,
        mock_get_secret,
    ):
        from communication.infra.vm_helpers import create_windows_vm

        def _secret_side_effect(name, **kw):
            return {
                "VM_WILDCARD_FULLCHAIN": _FAKE_CERT,
                "VM_WILDCARD_PRIVKEY": _FAKE_KEY,
                "DEVBOT_GITHUB_TOKEN": "ghp_fake",
            }.get(name)

        mock_get_secret.side_effect = _secret_side_effect
        mock_op = MagicMock()
        mock_client_cls.return_value.insert.return_value = mock_op

        create_windows_vm(**_COMMON_ARGS)

        insert_call = mock_client_cls.return_value.insert.call_args
        instance = insert_call.kwargs.get(
            "instance_resource",
            insert_call[1].get("instance_resource"),
        )
        meta = _extract_metadata_dict(instance)

        assert meta.get("tls-fullchain") == _FAKE_CERT
        assert meta.get("tls-privkey") == _FAKE_KEY

    @patch("communication.infra.vm_helpers.get_secret")
    @patch("communication.infra.vm_helpers.compute_v1.InstancesClient")
    def test_no_tls_metadata_when_cert_absent(
        self,
        mock_client_cls,
        mock_get_secret,
    ):
        from communication.infra.vm_helpers import create_windows_vm

        def _secret_side_effect(name, **kw):
            if name == "DEVBOT_GITHUB_TOKEN":
                return "ghp_fake"
            return None

        mock_get_secret.side_effect = _secret_side_effect
        mock_op = MagicMock()
        mock_client_cls.return_value.insert.return_value = mock_op

        create_windows_vm(**_COMMON_ARGS)

        insert_call = mock_client_cls.return_value.insert.call_args
        instance = insert_call.kwargs.get(
            "instance_resource",
            insert_call[1].get("instance_resource"),
        )
        meta = _extract_metadata_dict(instance)

        assert "tls-fullchain" not in meta
        assert "tls-privkey" not in meta


class TestUbuntuVmWildcardCert:

    @patch("communication.infra.vm_helpers.get_secret")
    @patch("communication.infra.vm_helpers.compute_v1.InstancesClient")
    def test_includes_tls_metadata_when_cert_available(
        self,
        mock_client_cls,
        mock_get_secret,
    ):
        from communication.infra.vm_helpers import create_ubuntu_vm

        def _secret_side_effect(name, **kw):
            return {
                "VM_WILDCARD_FULLCHAIN": _FAKE_CERT,
                "VM_WILDCARD_PRIVKEY": _FAKE_KEY,
                "DEVBOT_GITHUB_TOKEN": "ghp_fake",
            }.get(name)

        mock_get_secret.side_effect = _secret_side_effect
        mock_op = MagicMock()
        mock_client_cls.return_value.insert.return_value = mock_op

        create_ubuntu_vm(**_COMMON_ARGS)

        insert_call = mock_client_cls.return_value.insert.call_args
        instance = insert_call.kwargs.get(
            "instance_resource",
            insert_call[1].get("instance_resource"),
        )
        meta = _extract_metadata_dict(instance)

        assert meta.get("tls-fullchain") == _FAKE_CERT
        assert meta.get("tls-privkey") == _FAKE_KEY

    @patch("communication.infra.vm_helpers.get_secret")
    @patch("communication.infra.vm_helpers.compute_v1.InstancesClient")
    def test_no_tls_metadata_when_cert_absent(
        self,
        mock_client_cls,
        mock_get_secret,
    ):
        from communication.infra.vm_helpers import create_ubuntu_vm

        def _secret_side_effect(name, **kw):
            if name == "DEVBOT_GITHUB_TOKEN":
                return "ghp_fake"
            return None

        mock_get_secret.side_effect = _secret_side_effect
        mock_op = MagicMock()
        mock_client_cls.return_value.insert.return_value = mock_op

        create_ubuntu_vm(**_COMMON_ARGS)

        insert_call = mock_client_cls.return_value.insert.call_args
        instance = insert_call.kwargs.get(
            "instance_resource",
            insert_call[1].get("instance_resource"),
        )
        meta = _extract_metadata_dict(instance)

        assert "tls-fullchain" not in meta
        assert "tls-privkey" not in meta

    @patch("communication.infra.vm_helpers.get_secret")
    @patch("communication.infra.vm_helpers.compute_v1.InstancesClient")
    def test_vm_creation_succeeds_without_cert(
        self,
        mock_client_cls,
        mock_get_secret,
    ):
        """VM creation should not raise even when cert secrets are missing."""
        from communication.infra.vm_helpers import create_ubuntu_vm

        mock_get_secret.return_value = None
        mock_op = MagicMock()
        mock_client_cls.return_value.insert.return_value = mock_op

        result = create_ubuntu_vm(**_COMMON_ARGS)

        assert result["status"] == "RUNNING"
        assert result["hostname"] == _COMMON_ARGS["hostname"]
