def test_incident_masks_secrets(client, session) -> None:
    reg = client.post(
        "/v1/auth/register",
        json={
            "email": "rep@example.com",
            "password": "password-1234",
            "consent_pdn": True,
            "consent_training": False,
        },
    )
    token = reg.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    secret = "hunter2"
    response = client.post(
        "/v1/reports/incident",
        headers=headers,
        json={
            "message": "Something broke",
            "log_excerpt": f"login failed password={secret}",
        },
    )
    assert response.status_code == 201
    assert secret not in response.text
