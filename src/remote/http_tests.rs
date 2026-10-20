use super::*;
use crate::{
    auth::{AdminAuth, AuthConfig, hash_password},
    state::SessionRegistry,
};
use axum::{
    body::{Body, to_bytes},
    http::Request,
};
use serde_json::{Value, json};
use tower::ServiceExt;
async fn call(
    app: &Router,
    method: &str,
    path: &str,
    cookie: &str,
    action: &str,
    value: Value,
) -> Response {
    app.clone()
        .oneshot(
            Request::builder()
                .method(method)
                .uri(path)
                .header("Host", "platform.test")
                .header("Content-Type", "application/json")
                .header("Cookie", cookie)
                .header("X-MskDSP-Action", action)
                .body(Body::from(value.to_string()))
                .unwrap(),
        )
        .await
        .unwrap()
}
async fn json_body(response: Response) -> Value {
    serde_json::from_slice(&to_bytes(response.into_body(), 1024 * 1024).await.unwrap()).unwrap()
}
// 管理员权限与操作标记保护设备凭据；创建、授权、轮换、删除均可实际执行。
#[tokio::test]
async fn device_management_is_admin_only_and_token_is_not_listed() {
    let dir = tempfile::tempdir().unwrap();
    let store = DeviceStore::open(dir.path().join("devices.sqlite3"))
        .await
        .unwrap();
    let accounts = AccountStore::open(dir.path().join("accounts.sqlite3"))
        .await
        .unwrap();
    let account = accounts
        .create(crate::accounts::NewAccount {
            username: "operator".into(),
            password: "field-password-123".into(),
            display_name: "维护".into(),
        })
        .await
        .unwrap();
    let state = HttpState {
        auth: AdminAuth::new(AuthConfig {
            password_hash: hash_password("admin-test-password").unwrap(),
            ..AuthConfig::default()
        })
        .unwrap(),
        sessions: SessionRegistry::default(),
        protocol_version: "platform.v1".into(),
    };
    let app = crate::http::router(state.clone()).merge(router(
        state,
        store.clone(),
        accounts,
        Hub::default(),
    ));
    assert_eq!(
        call(&app, "GET", "/api/v1/devices", "", "", json!(null))
            .await
            .status(),
        StatusCode::UNAUTHORIZED
    );
    let login = call(
        &app,
        "POST",
        "/api/v1/auth/login",
        "",
        "auth",
        json!({"username":"root","password":"admin-test-password"}),
    )
    .await;
    assert_eq!(login.status(), StatusCode::OK);
    let cookie = login.headers()["set-cookie"]
        .to_str()
        .unwrap()
        .split(';')
        .next()
        .unwrap()
        .to_owned();
    assert_eq!(
        call(
            &app,
            "POST",
            "/api/v1/devices",
            &cookie,
            "",
            json!({"device_id":"field","display_name":"现场"})
        )
        .await
        .status(),
        StatusCode::FORBIDDEN
    );
    let created = call(
        &app,
        "POST",
        "/api/v1/devices",
        &cookie,
        "device",
        json!({"device_id":"field","display_name":"现场"}),
    )
    .await;
    assert_eq!(created.status(), StatusCode::CREATED);
    let created = json_body(created).await;
    let token = created["token"].as_str().unwrap();
    assert!(store.authenticate(token).await.is_ok());
    let listed =
        json_body(call(&app, "GET", "/api/v1/devices", &cookie, "", json!(null)).await).await;
    assert!(listed[0].get("token").is_none());
    assert!(listed[0].get("token_hash").is_none());
    let grants = json!([{"account_id":account.id,"allow_grpc":true,"allow_ssh":false}]);
    assert_eq!(
        call(
            &app,
            "PUT",
            "/api/v1/devices/field/grants",
            &cookie,
            "device",
            grants
        )
        .await
        .status(),
        StatusCode::NO_CONTENT
    );
    assert_eq!(
        call(
            &app,
            "PUT",
            "/api/v1/devices/field/grants",
            &cookie,
            "device",
            json!([{"account_id":"missing","allow_grpc":true,"allow_ssh":false}])
        )
        .await
        .status(),
        StatusCode::BAD_REQUEST
    );
    let rotated = json_body(
        call(
            &app,
            "POST",
            "/api/v1/devices/field/token",
            &cookie,
            "device",
            json!(null),
        )
        .await,
    )
    .await;
    assert!(store.authenticate(token).await.is_err());
    assert!(
        store
            .authenticate(rotated["token"].as_str().unwrap())
            .await
            .is_ok()
    );
    assert_eq!(
        call(
            &app,
            "DELETE",
            "/api/v1/devices/field",
            &cookie,
            "device",
            json!(null)
        )
        .await
        .status(),
        StatusCode::NO_CONTENT
    );
    assert!(store.list().await.unwrap().is_empty());
}
