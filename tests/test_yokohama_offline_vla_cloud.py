import copy
import datetime
import io
import json
import subprocess
import urllib.error

import pytest

from scripts import yokohama_offline_vla_cloud as cloud
from scripts import yokohama_offline_vla_lifecycle as safety


def fixture():
    vm = safety.OwnedVM(
        "cpu-project", "us-west1-b", "cpu-instance", "123", "cpu-instance", "456", "abcdefabcdef"
    )
    prefix = "https://www.googleapis.com/compute/v1/projects/cpu-project/zones/us-west1-b"
    instance = {
        "id": "123",
        "name": vm.name,
        "selfLink": prefix + "/instances/" + vm.name,
        "labels": {"trial-id": vm.trial_id, "managed-by": "yokohama-offline-vla"},
        "disks": [{"boot": True, "autoDelete": True, "source": prefix + "/disks/" + vm.name}],
    }
    disk = {
        "id": "456",
        "name": vm.name,
        "selfLink": prefix + "/disks/" + vm.name,
        "users": [instance["selfLink"]],
    }
    begin = 1700000000
    instance.update(
        creationTimestamp=datetime.datetime.fromtimestamp(
            begin + 10, datetime.timezone.utc
        ).isoformat(),
        machineType="https://www.googleapis.com/compute/v1/projects/cpu-project/zones/us-west1-b/machineTypes/g2-standard-16",
        scheduling={
            "instanceTerminationAction": "DELETE",
            "terminationTime": datetime.datetime.fromtimestamp(
                begin + 540, datetime.timezone.utc
            ).isoformat(),
        },
    )
    disk.update(
        sizeGb="200",
        type="https://www.googleapis.com/compute/v1/projects/cpu-project/zones/us-west1-b/diskTypes/pd-balanced",
    )
    config = {
        "project": vm.project,
        "zone": vm.zone,
        "instance": vm.name,
        "trial_id": vm.trial_id,
        "submission_unix_s": begin,
    }
    operation = {"operationType": "insert", "targetLink": instance["selfLink"], "status": "DONE"}
    return config, instance, disk, operation


def test_900s_quote_preserves_ledger_and_lower_transmit_limit():
    value = cloud.quote_900(
        prior_group_usd=0.4568919543032134,
        prior_cumulative_usd=1.751203792820632,
        hourly_usd=1.179605584,
        egress_usd_per_gib=0.23,
    )
    assert value["resource_estimate_USD"] == pytest.approx(0.409901396)
    assert value["new_estimate_with_contingency_USD"] == pytest.approx(0.4918816752)
    assert value["fits_existing_budget"]
    assert value["new_budget_allocation_USD"] == 0
    assert cloud.PROFILE["kernel_TX_threshold"] < cloud.PROFILE["maximum_egress_bytes"] <= 1024**3
    assert (
        cloud.PROFILE["preparation_s"]
        < cloud.PROFILE["comparison_end_s"]
        < cloud.PROFILE["primary_delete_s"]
        < cloud.PROFILE["watchdog_s"]
        < cloud.PROFILE["cloud_DELETE_s"]
        < 900
    )
    assert 900 - cloud.PROFILE["cloud_DELETE_s"] == 360


def test_over_budget_cannot_authorize_creation():
    value = cloud.quote_900(
        prior_group_usd=0.8, prior_cumulative_usd=2, hourly_usd=1.179605584, egress_usd_per_gib=0.23
    )
    assert not value["fits_existing_budget"]


@pytest.mark.parametrize("field", ["created", "termination", "id", "disk_type"])
def test_owner_binding_rejects_unreviewed_resource(field):
    config, instance, disk, _ = fixture()
    instance, disk = copy.deepcopy(instance), copy.deepcopy(disk)
    if field == "created":
        instance["creationTimestamp"] = "2020-01-01T00:00:00+00:00"
    if field == "termination":
        instance["scheduling"]["terminationTime"] = "2030-01-01T00:00:00+00:00"
    if field == "id":
        instance["labels"]["trial-id"] = "ffffffffffff"
    if field == "disk_type":
        disk["type"] = "unreviewed"
    with pytest.raises(ValueError):
        cloud.bind_owner(config, instance, disk)


def test_REST_actual_request_boundary_and_no_SDK_dependency(monkeypatch):
    config, instance, disk, operation = fixture()
    requests = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("REST cannot execute SDK"))

    def opener(request, timeout):
        requests.append(request)
        if "/operations?" in request.full_url:
            result = {"items": [operation]}
        elif "/disks/" in request.full_url:
            result = disk
        elif request.method == "DELETE":
            result = {"status": "PENDING", "operationType": "delete"}
        else:
            result = instance
        return io.BytesIO(json.dumps(result).encode())

    transport = cloud.RestTransport("CPU_FAKE_TOKEN", opener=opener)
    observed_instance, observed_disk, terminal = transport.observe(config)
    vm = cloud.bind_owner(config, observed_instance, observed_disk)
    assert terminal
    result = transport.delete(vm, safety.watchdog_delete_request(vm))
    assert result["operationType"] == "delete"
    assert [request.method for request in requests] == ["GET", "GET", "GET", "DELETE"]
    assert requests[-1].get_header("Authorization") == "Bearer CPU_FAKE_TOKEN"


@pytest.mark.parametrize("code", [401, 403, 404])
def test_REST_error_is_redacted_and_only_404_means_absence(code):
    def fail(request, timeout):
        raise urllib.error.HTTPError(request.full_url, code, "CPU_FAKE_TOKEN", {}, None)

    transport = cloud.RestTransport("CPU_FAKE_TOKEN", opener=fail)
    url = "https://compute.googleapis.com/compute/v1/projects/cpu-project/zones/us-west1-b/instances/cpu-instance"
    if code == 404:
        assert transport.request("GET", url) is None
    else:
        with pytest.raises(RuntimeError) as error:
            transport.request("GET", url)
        assert "CPU_FAKE_TOKEN" not in str(error.value)


def test_REST_cannot_create_or_call_other_service():
    transport = cloud.RestTransport("CPU_FAKE_TOKEN", opener=lambda *a, **k: pytest.fail("No call"))
    with pytest.raises(ValueError):
        transport.request(
            "POST",
            "https://compute.googleapis.com/compute/v1/projects/cpu-project/zones/us-west1-b/instances",
        )
    with pytest.raises(ValueError):
        transport.request("GET", "https://example.com/")


def test_SDK_delete_argument_and_timeout_boundaries(tmp_path):
    config, instance, disk, _ = fixture()
    vm = cloud.bind_owner(config, instance, disk)
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    transport = cloud.SDKTransport("CPU_FAKE_SDK", tmp_path, config, runner=runner)
    transport.delete(vm, safety.sdk_delete_arguments(vm))
    assert "--async" not in calls[0][0]
    assert calls[0][1]["timeout"] == 20
    with pytest.raises(ValueError):
        transport.delete(vm, [*safety.sdk_delete_arguments(vm), "--async"])
