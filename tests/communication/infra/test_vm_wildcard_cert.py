"""
Tests for wildcard TLS certificate distribution to pool VMs.

Verifies that provision_pool_vm() fetches the wildcard cert from Secret
Manager and includes it in VM metadata, and that provisioning proceeds
gracefully when the cert is not available.
"""

from unittest.mock import patch, MagicMock

_FAKE_CERT = "-----BEGIN CERTIFICATE-----\nMIIFake...\n-----END CERTIFICATE-----\n"
_FAKE_KEY = "-----BEGIN PRIVATE KEY-----\nMIIFake...\n-----END PRIVATE KEY-----\n"


def _extract_metadata_dict(instance_resource):
    """Pull the metadata items from a compute_v1.Instance into a dict."""
    return {item.key: item.value for item in instance_resource.metadata.items}


def _mock_provision(vm_type, mock_client_cls, mock_get_secret, mock_dns, mock_addr):
    """Run provision_pool_vm with standard mocks and return the Instance."""
    from communication.infra.vm_helpers import provision_pool_vm

    mock_op = MagicMock()
    mock_client_cls.return_value.insert.return_value = mock_op

    mock_addr_instance = MagicMock()
    mock_addr_instance.insert.return_value = mock_op
    mock_addr_instance.get.return_value = MagicMock(address="10.0.0.1")
    mock_addr.return_value = mock_addr_instance

    mock_zone = MagicMock()
    mock_zone.list_resource_record_sets.return_value = []
    mock_dns_client = MagicMock()
    mock_dns_client.zone.return_value = mock_zone
    mock_dns.return_value = mock_dns_client

    provision_pool_vm(vm_type, n=99)

    insert_call = mock_client_cls.return_value.insert.call_args
    instance = insert_call.kwargs.get(
        "instance_resource",
        insert_call[1].get("instance_resource"),
    )
    return _extract_metadata_dict(instance)


_COMMON_PATCHES = [
    "communication.infra.vm_helpers.compute_v1.AddressesClient",
    "communication.infra.vm_helpers.dns.Client",
    "communication.infra.vm_helpers.compute_v1.InstancesClient",
    "communication.infra.vm_helpers.get_secret",
]


class TestPoolVmWildcardCert:

    @patch(_COMMON_PATCHES[0])
    @patch(_COMMON_PATCHES[1])
    @patch(_COMMON_PATCHES[2])
    @patch(_COMMON_PATCHES[3])
    def test_ubuntu_includes_tls_when_available(
        self,
        mock_get_secret,
        mock_client_cls,
        mock_dns,
        mock_addr,
    ):
        def _secret(name, **kw):
            return {
                "VM_WILDCARD_FULLCHAIN": _FAKE_CERT,
                "VM_WILDCARD_PRIVKEY": _FAKE_KEY,
                "DEVBOT_GITHUB_TOKEN": "ghp_fake",
            }.get(name)

        mock_get_secret.side_effect = _secret
        meta = _mock_provision(
            "ubuntu",
            mock_client_cls,
            mock_get_secret,
            mock_dns,
            mock_addr,
        )

        assert meta.get("tls-fullchain") == _FAKE_CERT
        assert meta.get("tls-privkey") == _FAKE_KEY

    @patch(_COMMON_PATCHES[0])
    @patch(_COMMON_PATCHES[1])
    @patch(_COMMON_PATCHES[2])
    @patch(_COMMON_PATCHES[3])
    def test_ubuntu_no_tls_when_absent(
        self,
        mock_get_secret,
        mock_client_cls,
        mock_dns,
        mock_addr,
    ):
        def _secret(name, **kw):
            if name == "DEVBOT_GITHUB_TOKEN":
                return "ghp_fake"
            return None

        mock_get_secret.side_effect = _secret
        meta = _mock_provision(
            "ubuntu",
            mock_client_cls,
            mock_get_secret,
            mock_dns,
            mock_addr,
        )

        assert "tls-fullchain" not in meta
        assert "tls-privkey" not in meta

    @patch(_COMMON_PATCHES[0])
    @patch(_COMMON_PATCHES[1])
    @patch(_COMMON_PATCHES[2])
    @patch(_COMMON_PATCHES[3])
    def test_windows_includes_tls_when_available(
        self,
        mock_get_secret,
        mock_client_cls,
        mock_dns,
        mock_addr,
    ):
        def _secret(name, **kw):
            return {
                "VM_WILDCARD_FULLCHAIN": _FAKE_CERT,
                "VM_WILDCARD_PRIVKEY": _FAKE_KEY,
                "DEVBOT_GITHUB_TOKEN": "ghp_fake",
            }.get(name)

        mock_get_secret.side_effect = _secret
        meta = _mock_provision(
            "windows",
            mock_client_cls,
            mock_get_secret,
            mock_dns,
            mock_addr,
        )

        assert meta.get("tls-fullchain") == _FAKE_CERT
        assert meta.get("tls-privkey") == _FAKE_KEY

    @patch(_COMMON_PATCHES[0])
    @patch(_COMMON_PATCHES[1])
    @patch(_COMMON_PATCHES[2])
    @patch(_COMMON_PATCHES[3])
    def test_provision_returns_after_insert_is_accepted(
        self,
        mock_get_secret,
        mock_client_cls,
        mock_dns,
        mock_addr,
    ):
        from communication.infra.vm_helpers import provision_pool_vm

        def _secret(name, **kw):
            return {
                "VM_WILDCARD_FULLCHAIN": _FAKE_CERT,
                "VM_WILDCARD_PRIVKEY": _FAKE_KEY,
                "DEVBOT_GITHUB_TOKEN": "ghp_fake",
            }.get(name)

        mock_get_secret.side_effect = _secret

        address_op = MagicMock()
        mock_addr_instance = MagicMock()
        mock_addr_instance.insert.return_value = address_op
        mock_addr_instance.get.return_value = MagicMock(address="10.0.0.1")
        mock_addr.return_value = mock_addr_instance

        mock_zone = MagicMock()
        mock_zone.list_resource_record_sets.return_value = []
        mock_dns_client = MagicMock()
        mock_dns_client.zone.return_value = mock_zone
        mock_dns.return_value = mock_dns_client

        instance_op = MagicMock()
        instance_op.result.side_effect = AssertionError(
            "instance provisioning should not block on op.result()",
        )
        mock_client_cls.return_value.insert.return_value = instance_op

        result = provision_pool_vm("ubuntu", n=99)

        assert result["vm_name"] == "unity-pool-ubuntu-99"
        assert result["status"] == "PROVISIONING"
        mock_client_cls.return_value.insert.assert_called_once()

    @patch(_COMMON_PATCHES[0])
    @patch(_COMMON_PATCHES[1])
    @patch(_COMMON_PATCHES[2])
    @patch(_COMMON_PATCHES[3])
    def test_windows_no_tls_when_absent(
        self,
        mock_get_secret,
        mock_client_cls,
        mock_dns,
        mock_addr,
    ):
        def _secret(name, **kw):
            if name == "DEVBOT_GITHUB_TOKEN":
                return "ghp_fake"
            return None

        mock_get_secret.side_effect = _secret
        meta = _mock_provision(
            "windows",
            mock_client_cls,
            mock_get_secret,
            mock_dns,
            mock_addr,
        )

        assert "tls-fullchain" not in meta
        assert "tls-privkey" not in meta

    @patch(_COMMON_PATCHES[0])
    @patch(_COMMON_PATCHES[1])
    @patch(_COMMON_PATCHES[2])
    @patch(_COMMON_PATCHES[3])
    def test_provision_waits_for_static_ip_before_creating_dns_record(
        self,
        mock_get_secret,
        mock_client_cls,
        mock_dns,
        mock_addr,
    ):
        from communication.infra.vm_helpers import _pool_hostname, provision_pool_vm

        def _secret(name, **kw):
            return {
                "VM_WILDCARD_FULLCHAIN": _FAKE_CERT,
                "VM_WILDCARD_PRIVKEY": _FAKE_KEY,
                "DEVBOT_GITHUB_TOKEN": "ghp_fake",
            }.get(name)

        mock_get_secret.side_effect = _secret

        address_op = MagicMock()
        mock_addr_instance = MagicMock()
        mock_addr_instance.insert.return_value = address_op
        mock_addr_instance.get.side_effect = [
            MagicMock(address="", status="RESERVING"),
            MagicMock(address="10.0.0.1", status="RESERVED"),
        ]
        mock_addr.return_value = mock_addr_instance

        mock_zone = MagicMock()
        mock_zone.list_resource_record_sets.return_value = []
        mock_dns_client = MagicMock()
        mock_dns_client.zone.return_value = mock_zone
        mock_dns.return_value = mock_dns_client

        instance_op = MagicMock()
        mock_client_cls.return_value.insert.return_value = instance_op

        with patch("communication.infra.vm_helpers.time.sleep", return_value=None):
            provision_pool_vm("ubuntu", n=99)

        expected_fqdn = f"{_pool_hostname('ubuntu', 99)}."
        mock_zone.resource_record_set.assert_called_once_with(
            expected_fqdn,
            "A",
            300,
            ["10.0.0.1"],
        )
        assert mock_addr_instance.get.call_count == 2
