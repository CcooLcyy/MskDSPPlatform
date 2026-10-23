use super::store::*;

// 默认授权与设备原子落盘，授权写入中途失败不能留下不可用的设备身份。
#[tokio::test]
async fn device_and_initial_grants_are_atomic() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("devices.sqlite3");
    let store = DeviceStore::open(path.clone()).await.unwrap();
    let db = rusqlite::Connection::open(&path).unwrap();
    db.execute_batch("CREATE TRIGGER reject_grant BEFORE INSERT ON grants WHEN NEW.account_id='rejected' BEGIN SELECT RAISE(ABORT,'授权写入失败'); END;").unwrap();
    let input = || NewDevice {
        device_id: "new-device".into(),
        display_name: "新设备".into(),
    };
    let grants = || {
        vec![
            Grant {
                account_id: "allowed".into(),
                allow_grpc: true,
                allow_ssh: true,
            },
            Grant {
                account_id: "rejected".into(),
                allow_grpc: true,
                allow_ssh: true,
            },
        ]
    };
    assert!(matches!(
        store.create(input(), grants()).await,
        Err(StoreError::Storage)
    ));
    assert!(store.list().await.unwrap().is_empty());
    let count: i64 = db
        .query_row("SELECT count(*) FROM grants", [], |row| row.get(0))
        .unwrap();
    assert_eq!(count, 0);
    db.execute_batch("DROP TRIGGER reject_grant;").unwrap();
    store.create(input(), grants()).await.unwrap();
    drop(store);
    let reopened = DeviceStore::open(path).await.unwrap();
    assert_eq!(reopened.grants("new-device").await.unwrap().len(), 2);
    for account in ["allowed", "rejected"] {
        reopened
            .authorize("new-device", account, "ModuleManager")
            .await
            .unwrap();
        reopened
            .authorize("new-device", account, "ssh")
            .await
            .unwrap();
    }
}

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
    let first = store.create(input(), vec![]).await.unwrap();
    let second = store.create(input(), vec![]).await.unwrap();
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
            store.create(input, vec![]).await,
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
        .create(
            NewDevice {
                device_id: "field-01".into(),
                display_name: "现场一".into(),
            },
            vec![],
        )
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
        .create(
            NewDevice {
                device_id: "device".into(),
                display_name: "现场".into(),
            },
            vec![],
        )
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
        ("future.sqlite3", "PRAGMA user_version=99;"),
        ("missing.sqlite3", "PRAGMA user_version=1;"),
    ] {
        let path = dir.path().join(file);
        let db = rusqlite::Connection::open(&path).unwrap();
        db.execute_batch(sql).unwrap();
        drop(db);
        assert!(DeviceStore::open(path).await.is_err());
    }
}

// 升级保留版本一授权记录但全部待确认；明确保存后恢复权限且重开不会再次撤权。
#[tokio::test]
async fn legacy_grants_require_explicit_confirmation_and_survive_reopen() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("legacy.sqlite3");
    let db = rusqlite::Connection::open(&path).unwrap();
    db.execute_batch(
        "CREATE TABLE devices(id TEXT PRIMARY KEY NOT NULL,display_name TEXT NOT NULL,enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),token_hash TEXT NOT NULL UNIQUE,created_at INTEGER NOT NULL);
         CREATE TABLE grants(device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,account_id TEXT NOT NULL,allow_grpc INTEGER NOT NULL CHECK(allow_grpc IN (0,1)),allow_ssh INTEGER NOT NULL CHECK(allow_ssh IN (0,1)),PRIMARY KEY(device_id,account_id));
         INSERT INTO devices VALUES('legacy','历史设备',1,'historical-token-hash',1);
         INSERT INTO grants VALUES('legacy','operator',1,1);
         PRAGMA user_version=1;"
    ).unwrap();
    let store = DeviceStore::open(path.clone()).await.unwrap();
    assert!(store.grants("legacy").await.unwrap().is_empty());
    assert!(store.for_account("operator").await.unwrap().is_empty());
    for service in ["ModuleManager", "ssh"] {
        assert!(matches!(
            store.authorize("legacy", "operator", service).await,
            Err(StoreError::Denied)
        ));
    }
    let count: i64 = db
        .query_row("SELECT count(*) FROM grants WHERE confirmed=0", [], |r| {
            r.get(0)
        })
        .unwrap();
    assert_eq!(count, 1, "历史授权记录必须保留待确认");
    let version: i64 = db
        .pragma_query_value(None, "user_version", |r| r.get(0))
        .unwrap();
    assert_eq!(version, 2);
    store
        .set_grants(
            "legacy",
            vec![Grant {
                account_id: "operator".into(),
                allow_grpc: true,
                allow_ssh: false,
            }],
        )
        .await
        .unwrap();
    drop(store);
    let reopened = DeviceStore::open(path).await.unwrap();
    assert_eq!(reopened.grants("legacy").await.unwrap().len(), 1);
    assert_eq!(reopened.for_account("operator").await.unwrap().len(), 1);
    reopened
        .authorize("legacy", "operator", "ModuleManager")
        .await
        .unwrap();
    assert!(matches!(
        reopened.authorize("legacy", "operator", "ssh").await,
        Err(StoreError::Denied)
    ));
}

// 损坏版本一结构必须原子拒绝升级，不能留下新版本号或部分新增列。
#[tokio::test]
async fn invalid_legacy_schema_is_not_partially_migrated() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("broken-legacy.sqlite3");
    let db = rusqlite::Connection::open(&path).unwrap();
    db.execute_batch(
        "CREATE TABLE devices(id TEXT PRIMARY KEY,display_name TEXT,enabled INTEGER,token_hash TEXT,created_at INTEGER);
         CREATE TABLE grants(device_id TEXT,account_id TEXT,allow_grpc INTEGER);
         PRAGMA user_version=1;"
    ).unwrap();
    assert!(DeviceStore::open(path).await.is_err());
    let version: i64 = db
        .pragma_query_value(None, "user_version", |r| r.get(0))
        .unwrap();
    assert_eq!(version, 1);
    let mut statement = db.prepare("PRAGMA table_info(grants)").unwrap();
    let columns: Vec<String> = statement
        .query_map([], |r| r.get(1))
        .unwrap()
        .collect::<Result<_, _>>()
        .unwrap();
    assert!(!columns.iter().any(|column| column == "confirmed"));
}
