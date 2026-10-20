use super::store::*;

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
