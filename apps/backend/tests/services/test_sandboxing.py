from src.services.sandboxing import get_sandbox_options


def test_sandbox_drops_capabilities_and_uses_no_new_privileges():
    options = get_sandbox_options("run-123")

    assert options["cap_drop"] == ["ALL"]
    assert options["security_opt"] == ["no-new-privileges:true"]
    assert options["read_only"] is True
    assert "uid=10001" in options["tmpfs"]["/workspace"]
    assert "gid=10001" in options["tmpfs"]["/workspace"]


def test_host_gateway_is_opt_in(monkeypatch):
    monkeypatch.setattr("src.services.sandboxing.settings.agent_allow_host_gateway", False)
    assert "extra_hosts" not in get_sandbox_options("run-123")

    monkeypatch.setattr("src.services.sandboxing.settings.agent_allow_host_gateway", True)
    assert get_sandbox_options("run-123")["extra_hosts"] == {
        "host.docker.internal": "host-gateway"
    }


def test_sandbox_disables_host_gateway_by_default(settings=None):
    from src.services import sandboxing

    opts = sandboxing.get_sandbox_options("00000000-0000-0000-0000-000000000000")
    assert opts.get("extra_hosts") is None or "host.docker.internal" not in str(
        opts.get("extra_hosts")
    )
    assert opts["read_only"] is True
    assert "ALL" in opts["cap_drop"]
