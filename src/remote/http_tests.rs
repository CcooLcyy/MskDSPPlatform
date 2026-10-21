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
async fn admin_app(store: DeviceStore, accounts: AccountStore) -> (Router, String) {
    let state = HttpState {
        auth: AdminAuth::new(AuthConfig {
            password_hash: hash_password("admin-test-password").unwrap(),
            ..AuthConfig::default()
        })
        .unwrap(),
        sessions: SessionRegistry::default(),
        protocol_version: "platform.v1".into(),
    };
    let app =
        crate::http::router(state.clone()).merge(router(state, store, accounts, Hub::default()));
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
    (app, cookie)
}

// 创建时为全部已有账号持久化两项权限；停用账号仍不能登录，旧授权与后建账号不受影响。
#[tokio::test]
async fn device_creation_grants_existing_accounts_without_changing_other_permissions() {
    let dir = tempfile::tempdir().unwrap();
    let device_db = dir.path().join("devices.sqlite3");
    let store = DeviceStore::open(device_db.clone()).await.unwrap();
    let accounts = AccountStore::open(dir.path().join("accounts.sqlite3"))
        .await
        .unwrap();
    let mut existing_accounts = Vec::new();
    for username in ["operator-one", "operator-two", "disabled-operator"] {
        existing_accounts.push(
            accounts
                .create(crate::accounts::NewAccount {
                    username: username.into(),
                    password: "field-password-123".into(),
                    display_name: username.into(),
                })
                .await
                .unwrap(),
        );
    }
    let disabled = &existing_accounts[2];
    accounts
        .update(
            &disabled.id,
            disabled.revision,
            crate::accounts::AccountUpdate {
                display_name: disabled.display_name.clone(),
                enabled: false,
            },
        )
        .await
        .unwrap();
    let custom_grants = vec![Grant {
        account_id: existing_accounts[0].id.clone(),
        allow_grpc: false,
        allow_ssh: true,
    }];
    store
        .create(
            NewDevice {
                device_id: "old-device".into(),
                display_name: "原有设备".into(),
            },
            custom_grants.clone(),
        )
        .await
        .unwrap();
    store
        .create(
            NewDevice {
                device_id: "old-ungranted-device".into(),
                display_name: "原有未授权设备".into(),
            },
            vec![],
        )
        .await
        .unwrap();
    let (app, cookie) = admin_app(store, accounts.clone()).await;
    let created = call(
        &app,
        "POST",
        "/api/v1/devices",
        &cookie,
        "device",
        json!({"display_name":"研发测试"}),
    )
    .await;
    assert_eq!(created.status(), StatusCode::CREATED);
    let created = json_body(created).await;
    let id = created["device_id"].as_str().unwrap();
    let grants_path = format!("/api/v1/devices/{id}/grants");
    let mut expected = existing_accounts
        .iter()
        .map(|account| Grant {
            account_id: account.id.clone(),
            allow_grpc: true,
            allow_ssh: true,
        })
        .collect::<Vec<_>>();
    expected.sort_by(|left, right| left.account_id.cmp(&right.account_id));
    let response = call(&app, "GET", &grants_path, &cookie, "", json!(null)).await;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(json_body(response).await, json!(expected));

    // 重新打开数据库，确认权限在创建成功时已经落盘。
    let reopened = DeviceStore::open(device_db).await.unwrap();
    assert_eq!(json!(reopened.grants(id).await.unwrap()), json!(expected));
    for account in &existing_accounts {
        reopened
            .authorize(id, &account.id, "DataCenter")
            .await
            .unwrap();
        reopened.authorize(id, &account.id, "ssh").await.unwrap();
    }
    assert!(
        accounts
            .verify("operator-one", "field-password-123")
            .await
            .is_ok()
    );
    assert!(matches!(
        accounts
            .verify("disabled-operator", "field-password-123")
            .await,
        Err(crate::accounts::AccountError::Unauthorized)
    ));
    assert_eq!(
        json!(reopened.grants("old-device").await.unwrap()),
        json!(custom_grants)
    );
    assert!(
        reopened
            .grants("old-ungranted-device")
            .await
            .unwrap()
            .is_empty()
    );

    let later = accounts
        .create(crate::accounts::NewAccount {
            username: "later-operator".into(),
            password: "field-password-123".into(),
            display_name: "后建账号".into(),
        })
        .await
        .unwrap();
    assert_eq!(json!(reopened.grants(id).await.unwrap()), json!(expected));
    assert!(reopened.for_account(&later.id).await.unwrap().is_empty());
    for service in ["DataCenter", "ssh"] {
        assert!(matches!(
            reopened.authorize(id, &later.id, service).await,
            Err(StoreError::Denied)
        ));
    }
}

// 没有上位机账号时仍可创建设备，设备凭据有效且授权为空。
#[tokio::test]
async fn device_creation_without_accounts_keeps_empty_grants() {
    let dir = tempfile::tempdir().unwrap();
    let device_db = dir.path().join("devices.sqlite3");
    let store = DeviceStore::open(device_db.clone()).await.unwrap();
    let accounts = AccountStore::open(dir.path().join("accounts.sqlite3"))
        .await
        .unwrap();
    let (app, cookie) = admin_app(store, accounts).await;
    let created = call(
        &app,
        "POST",
        "/api/v1/devices",
        &cookie,
        "device",
        json!({"device_id":"first-device","display_name":"首台设备"}),
    )
    .await;
    assert_eq!(created.status(), StatusCode::CREATED);
    let created = json_body(created).await;
    assert_eq!(created["device_id"], "first-device");
    let reopened = DeviceStore::open(device_db).await.unwrap();
    assert!(reopened.grants("first-device").await.unwrap().is_empty());
    assert_eq!(
        reopened
            .authenticate(created["token"].as_str().unwrap())
            .await
            .unwrap()
            .device_id,
        "first-device"
    );
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
        json!({"display_name":"现场"}),
    )
    .await;
    assert_eq!(created.status(), StatusCode::CREATED);
    let created = json_body(created).await;
    let id = created["device_id"].as_str().unwrap();
    assert!(id.starts_with("device-"));
    assert_eq!(created["display_name"], "现场");
    assert_eq!(created["enabled"], true);
    let device_path = format!("/api/v1/devices/{id}");
    let grants_path = format!("{device_path}/grants");
    let token_path = format!("{device_path}/token");
    let token = created["token"].as_str().unwrap();
    assert_eq!(store.authenticate(token).await.unwrap().device_id, id);
    assert_eq!(created["token"].as_str().unwrap().len(), 64);
    assert_eq!(
        call(&app, "GET", &device_path, &cookie, "", json!(null))
            .await
            .status(),
        StatusCode::OK
    );
    let listed =
        json_body(call(&app, "GET", "/api/v1/devices", &cookie, "", json!(null)).await).await;
    assert!(listed[0].get("token").is_none());
    assert!(listed[0].get("token_hash").is_none());
    assert_eq!(listed[0]["device_id"], id);
    let grants = json!([{"account_id":account.id,"allow_grpc":true,"allow_ssh":false}]);
    assert_eq!(
        call(&app, "PUT", &grants_path, &cookie, "device", grants)
            .await
            .status(),
        StatusCode::NO_CONTENT
    );
    assert_eq!(
        call(
            &app,
            "PUT",
            &grants_path,
            &cookie,
            "device",
            json!([{"account_id":"missing","allow_grpc":true,"allow_ssh":false}])
        )
        .await
        .status(),
        StatusCode::BAD_REQUEST
    );
    let rotated =
        json_body(call(&app, "POST", &token_path, &cookie, "device", json!(null)).await).await;
    assert_eq!(rotated["device_id"], id);
    assert!(store.authenticate(token).await.is_err());
    assert!(
        store
            .authenticate(rotated["token"].as_str().unwrap())
            .await
            .is_ok()
    );
    assert_eq!(
        call(&app, "DELETE", &device_path, &cookie, "device", json!(null))
            .await
            .status(),
        StatusCode::NO_CONTENT
    );
    assert!(store.list().await.unwrap().is_empty());

    // 兼容既有 API 调用，显式设备 ID 继续原样保存。
    let legacy = call(
        &app,
        "POST",
        "/api/v1/devices",
        &cookie,
        "device",
        json!({"device_id":"field","display_name":"已有接口"}),
    )
    .await;
    assert_eq!(legacy.status(), StatusCode::CREATED);
    assert_eq!(json_body(legacy).await["device_id"], "field");
}
