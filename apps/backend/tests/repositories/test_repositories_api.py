import uuid


def test_list_repositories_returns_own_only(client, repository, other_repository):
    resp = client.get("/repositories/")
    assert resp.status_code == 200
    ids = [r["id"] for r in resp.json()]
    assert str(repository.id) in ids
    assert str(other_repository.id) not in ids


def test_create_repository_invalid_body_returns_422(client):
    resp = client.post("/repositories/", json={})
    assert resp.status_code == 422


def test_delete_nonexistent_repository_returns_404(client):
    resp = client.delete(f"/repositories/{uuid.uuid4()}")
    assert resp.status_code == 404
