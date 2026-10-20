use super::store::*;

// 仅提供中文名称时生成独立 ID；改名、轮换及重新打开数据库均保持身份。
#[tokio::test]
async fn generated_device_ids_are_independent_and_persist() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("devices.sqlite3");
    let store = DeviceStore::open(path.clone()).await.unwrap();
    let input = || {
        serde_json::from_value::<NewDevice>(serde_json::json!({
            "display_name": "一号光伏网关"
        }))
        .unwrap()
    };
    let first = store.create(input()).await.unwrap();
    let second = store.create(input()).await.unwrap();
    let id = first.device.device_id.clone();
    assert!(id.starts_with("device-"));
    assert_eq!(id.len(), 39);
    assert!(valid_service(&id));
    assert_ne!(id, second.device.device_id);
    assert_ne!(first.token, second.token);
    assert_eq!(first.device.display_name, "一号光伏网关");
    assert_eq!(
        store.authenticate(&first.token).await.unwrap().device_id,
        id
    );
    let renamed = store
        .update(
            &id,
            DeviceUpdate {
                display_name: "更名后的网关".into(),
                enabled: true,
            },
        )
        .await
        .unwrap();
    assert_eq!(renamed.device_id, id);
    let rotated = store.rotate(&id).await.unwrap();
    assert_eq!(rotated.device.device_id, id);
    assert!(store.authenticate(&first.token).await.is_err());
    drop(store);
    let reopened = DeviceStore::open(path).await.unwrap();
    assert_eq!(
        reopened
            .authenticate(&rotated.token)
            .await
            .unwrap()
            .device_id,
        id
    );
    assert_eq!(
        reopened.get(&id).await.unwrap().display_name,
        "更名后的网关"
    );
}

// 显式空标识或非法标识仍拒绝，不因省略标识的兼容逻辑而绕过校验。
#[tokio::test]
async fn explicit_invalid_device_ids_are_still_rejected() {
    let dir = tempfile::tempdir().unwrap();
    let store = DeviceStore::open(dir.path().join("devices.sqlite3"))
        .await
        .unwrap();
    for id in ["", "中文标识", "a/b", "a\nb"] {
        let input = serde_json::from_value::<NewDevice>(serde_json::json!({
            "device_id": id, "display_name": "网关"
        }))
        .unwrap();
        assert!(matches!(
            store.create(input).await,
            Err(StoreError::Invalid(_))
        ));
    }
    assert!(store.list().await.unwrap().is_empty());
}

// 设备凭据仅返回一次，数据库保存摘要；授权按账号隔离并持久化。
#[tokio::test]
async fn credentials_grants_persist_and_rotate() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("devices.sqlite3");
    let store = DeviceStore::open(path.clone()).await.unwrap();
    let created = store
        .create(NewDevice {
            device_id: "field-01".into(),
            display_name: "现场一".into(),
        })
        .await
        .unwrap();
    assert!(created.token.len() >= 32);
    assert_eq!(
        store.authenticate(&created.token).await.unwrap().device_id,
        "field-01"
    );
    assert!(store.authenticate("bad-token").await.is_err());
    store
        .set_grants(
            "field-01",
            vec![Grant {
                account_id: "account-1".into(),
                allow_grpc: true,
                allow_ssh: false,
            }],
        )
        .await
        .unwrap();
    assert!(
        store
            .authorize("field-01", "account-1", "ModuleManager")
            .await
            .is_ok()
    );
    assert!(
        store
            .authorize("field-01", "account-1", "ssh")
            .await
            .is_err()
    );
    assert!(
        store
            .authorize("field-01", "account-2", "ModuleManager")
            .await
            .is_err()
    );
    drop(store);
    let store = DeviceStore::open(path).await.unwrap();
    assert_eq!(store.for_account("account-1").await.unwrap().len(), 1);
    let rotated = store.rotate("field-01").await.unwrap();
    assert!(store.authenticate(&created.token).await.is_err());
    assert!(store.authenticate(&rotated.token).await.is_ok());
    store
        .update(
            "field-01",
            DeviceUpdate {
                display_name: "现场一".into(),
                enabled: false,
            },
        )
        .await
        .unwrap();
    assert!(store.authenticate(&rotated.token).await.is_err());
    assert!(
        store
            .authorize("field-01", "account-1", "ModuleManager")
            .await
            .is_err()
    );
}

// 服务标识禁止目的地址、路径及控制字符，防止将隧道扩展为任意代理。
#[test]
fn service_names_are_logical_identifiers() {
    for value in ["ModuleManager", "DataCenter", "ssh", "IEC104", "Modbus-1"] {
        assert!(valid_service(value));
    }
    for value in [
        "",
        "127.0.0.1:22",
        "http://localhost",
        "../ssh",
        "a/b",
        "a\nb",
    ] {
        assert!(!valid_service(value));
    }
}

// 高熵令牌只存摘要；重复授权输入失败时不能先清空现有授权。
#[tokio::test]
async fn token_is_hashed_and_invalid_grants_preserve_previous_values() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("devices.sqlite3");
    let store = DeviceStore::open(path.clone()).await.unwrap();
    let created = store
        .create(NewDevice {
            device_id: "device".into(),
            display_name: "现场".into(),
        })
        .await
        .unwrap();
    let db = rusqlite::Connection::open(path).unwrap();
    let hash: String = db
        .query_row("SELECT token_hash FROM devices", [], |r| r.get(0))
        .unwrap();
    assert_eq!(hash.len(), 64);
    assert_ne!(hash, created.token);
    let grant = Grant {
        account_id: "operator".into(),
        allow_grpc: true,
        allow_ssh: false,
    };
    store
        .set_grants("device", vec![grant.clone()])
        .await
        .unwrap();
    assert!(
        store
            .set_grants("device", vec![grant.clone(), grant])
            .await
            .is_err()
    );
    assert_eq!(store.grants("device").await.unwrap().len(), 1);
}

// 未知版本及缺少结构的数据库必须拒绝启动，不能重建为空库掩盖损坏。
#[tokio::test]
async fn invalid_database_schema_is_rejected() {
    let dir = tempfile::tempdir().unwrap();
    for (file, sql) in [
        ("future.sqlite3", "PRAGMA user_version=2;"),
        ("missing.sqlite3", "PRAGMA user_version=1;"),
    ] {
        let path = dir.path().join(file);
        let db = rusqlite::Connection::open(&path).unwrap();
        db.execute_batch(sql).unwrap();
        drop(db);
        assert!(DeviceStore::open(path).await.is_err());
    }
}
