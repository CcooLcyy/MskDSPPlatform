use super::*;
use crate::accounts::NewAccount;

async fn fixture() -> (tempfile::TempDir, AccountStore, ClientAuth) {
    let directory = tempfile::tempdir().unwrap();
    let store = AccountStore::open(directory.path().join("accounts.sqlite3"))
        .await
        .unwrap();
    let account: NewAccount = serde_json::from_value(serde_json::json!({
        "username": "persistent-user", "display_name": "更新会话测试", "password": "field-password-123"
    })).unwrap();
    store.create(account).await.unwrap();
    let auth = ClientAuth::new(store.clone(), 28_800);
    (directory, store, auth)
}

// 重新打开数据库模拟平台更新，原令牌仍有效且重启不延长到期时间。
#[tokio::test]
async fn reopening_database_keeps_token_without_renewing_expiry() {
    let (directory, _store, auth) = fixture().await;
    let login = auth
        .login("persistent-user", "field-password-123")
        .await
        .unwrap();
    let before = auth
        .authenticate(&login.access_token)
        .await
        .unwrap()
        .expires_in;
    drop(auth);
    let reopened = AccountStore::open(directory.path().join("accounts.sqlite3"))
        .await
        .unwrap();
    let restarted = ClientAuth::new(reopened, 86_400);
    let session = restarted.authenticate(&login.access_token).await.unwrap();
    assert_eq!(session.account.id, login.account.id);
    assert!(session.expires_in <= before);
    assert!(session.expires_in > 28_700);
}

// 通过新实例退出后，原实例及再次重启都不能复活同一登录令牌。
#[tokio::test]
async fn logout_after_restart_revokes_the_original_instance_too() {
    let (directory, _store, auth) = fixture().await;
    let login = auth
        .login("persistent-user", "field-password-123")
        .await
        .unwrap();
    let reopened = AccountStore::open(directory.path().join("accounts.sqlite3"))
        .await
        .unwrap();
    let restarted = ClientAuth::new(reopened, 28_800);
    restarted.authenticate(&login.access_token).await.unwrap();
    restarted.logout(&login.access_token).await.unwrap();
    assert!(matches!(
        auth.authenticate(&login.access_token).await,
        Err(ClientAuthError::Unauthorized)
    ));
    let reopened = AccountStore::open(directory.path().join("accounts.sqlite3"))
        .await
        .unwrap();
    assert!(matches!(
        ClientAuth::new(reopened, 28_800)
            .authenticate(&login.access_token)
            .await,
        Err(ClientAuthError::Unauthorized)
    ));
}

// 会话落盘仅包含摘要，保存期限使用固定绝对时间。
#[tokio::test]
async fn session_storage_contains_only_digest_and_absolute_expiry() {
    let (directory, _store, auth) = fixture().await;
    let login = auth
        .login("persistent-user", "field-password-123")
        .await
        .unwrap();
    let db = rusqlite::Connection::open(directory.path().join("accounts.sqlite3")).unwrap();
    let (hash, issued, expires): (String, i64, i64) = db
        .query_row(
            "SELECT token_hash,issued_at,expires_at FROM client_sessions_v1",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .unwrap();
    assert_eq!(hash, token_digest(&login.access_token));
    assert_ne!(hash, login.access_token);
    assert_eq!(expires - issued, 28_800_000);
    let mut stmt = db
        .prepare("SELECT sql FROM sqlite_master WHERE name='client_sessions_v1'")
        .unwrap();
    let definition: String = stmt.query_row([], |r| r.get(0)).unwrap();
    assert!(!definition.contains("password"));
    assert!(!definition.contains("access_token"));
}

// 过期记录重新打开数据库后仍无效，登录会清理过期记录并释放容量。
#[tokio::test]
async fn expiry_survives_restart_and_frees_capacity_on_login() {
    let (directory, _store, auth) = fixture().await;
    let login = auth
        .login("persistent-user", "field-password-123")
        .await
        .unwrap();
    assert!(auth.expire_session_for_test(&login.access_token).await);
    let store = AccountStore::open(directory.path().join("accounts.sqlite3"))
        .await
        .unwrap();
    let restarted = ClientAuth::new(store, 28_800);
    assert!(matches!(
        restarted.authenticate(&login.access_token).await,
        Err(ClientAuthError::Unauthorized)
    ));
    let fresh = restarted
        .login("persistent-user", "field-password-123")
        .await
        .unwrap();
    assert!(restarted.authenticate(&fresh.access_token).await.is_ok());
    let db = rusqlite::Connection::open(directory.path().join("accounts.sqlite3")).unwrap();
    assert_eq!(
        db.query_row("SELECT count(*) FROM client_sessions_v1", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        1
    );
}

// 禁用后重新启用、重置密码及再次打开数据库都不能恢复旧令牌。
#[tokio::test]
async fn account_revocation_survives_reopening_database() {
    let (directory, store, auth) = fixture().await;
    let first = auth
        .login("persistent-user", "field-password-123")
        .await
        .unwrap();
    for enabled in [false, true] {
        let old = store.get(&first.account.id).await.unwrap();
        let update = serde_json::from_value(
            serde_json::json!({"display_name": "更新会话测试", "enabled": enabled}),
        )
        .unwrap();
        store.update(&old.id, old.revision, update).await.unwrap();
        let reopened = AccountStore::open(directory.path().join("accounts.sqlite3"))
            .await
            .unwrap();
        assert!(matches!(
            ClientAuth::new(reopened, 28_800)
                .authenticate(&first.access_token)
                .await,
            Err(ClientAuthError::Unauthorized)
        ));
    }
    let second = auth
        .login("persistent-user", "field-password-123")
        .await
        .unwrap();
    store
        .reset_password(
            &second.account.id,
            second.account.revision,
            "changed-password-123".into(),
        )
        .await
        .unwrap();
    let reopened = AccountStore::open(directory.path().join("accounts.sqlite3"))
        .await
        .unwrap();
    let restarted = ClientAuth::new(reopened, 28_800);
    assert!(matches!(
        restarted.authenticate(&second.access_token).await,
        Err(ClientAuthError::Unauthorized)
    ));
    assert!(
        restarted
            .login("persistent-user", "changed-password-123")
            .await
            .is_ok()
    );
}

// 数据库故障时退出明确失败，恢复存储后原会话仍然可用。
#[tokio::test]
async fn logout_storage_failure_does_not_claim_revocation() {
    let (directory, _store, auth) = fixture().await;
    let login = auth
        .login("persistent-user", "field-password-123")
        .await
        .unwrap();
    let db = rusqlite::Connection::open(directory.path().join("accounts.sqlite3")).unwrap();
    db.execute_batch("ALTER TABLE client_sessions_v1 RENAME TO unavailable_sessions")
        .unwrap();
    assert!(matches!(
        auth.logout(&login.access_token).await,
        Err(ClientAuthError::Storage)
    ));
    assert!(matches!(
        auth.authenticate(&login.access_token).await,
        Err(ClientAuthError::Storage)
    ));
    db.execute_batch("ALTER TABLE unavailable_sessions RENAME TO client_sessions_v1")
        .unwrap();
    assert!(auth.authenticate(&login.access_token).await.is_ok());
    auth.logout(&login.access_token).await.unwrap();
}

// 两个独立认证实例争用最后一个名额时，事务保证只有一个登录成功。
#[tokio::test]
async fn concurrent_logins_after_restart_respect_shared_capacity() {
    let (directory, _store, auth) = fixture().await;
    let login = auth
        .login("persistent-user", "field-password-123")
        .await
        .unwrap();
    let db = rusqlite::Connection::open(directory.path().join("accounts.sqlite3")).unwrap();
    let now = chrono::Utc::now().timestamp_millis();
    db.execute(
        "WITH RECURSIVE slots(n) AS (SELECT 0 UNION ALL SELECT n+1 FROM slots WHERE n<13)
         INSERT INTO client_sessions_v1(token_hash,account_id,auth_version,issued_at,expires_at,last_seen_at)
         SELECT printf('%064x',n),?1,?2,?3,?4,?3 FROM slots",
        rusqlite::params![
            login.account.id,
            login.account.auth_version,
            now,
            now + 100_000
        ],
    )
    .unwrap();
    let other_store = AccountStore::open(directory.path().join("accounts.sqlite3"))
        .await
        .unwrap();
    let other = ClientAuth::new(other_store, 28_800);
    let (one, two) = tokio::join!(
        auth.login("persistent-user", "field-password-123"),
        other.login("persistent-user", "field-password-123")
    );
    assert_eq!(usize::from(one.is_ok()) + usize::from(two.is_ok()), 1);
    assert!(
        matches!(one, Err(ClientAuthError::SessionLimit))
            || matches!(two, Err(ClientAuthError::SessionLimit))
    );
    assert_eq!(
        db.query_row("SELECT count(*) FROM client_sessions_v1", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        16
    );

    let remaining = one.or(two).unwrap();
    db.execute(
        "UPDATE client_sessions_v1 SET last_seen_at=?1 WHERE token_hash=?2",
        rusqlite::params![
            chrono::Utc::now().timestamp_millis() - 121_000,
            token_digest(&login.access_token)
        ],
    )
    .unwrap();
    assert!(matches!(
        auth.authenticate(&login.access_token).await,
        Err(ClientAuthError::Unauthorized)
    ));
    assert!(
        auth.login("persistent-user", "field-password-123")
            .await
            .is_ok()
    );
    assert!(auth.authenticate(&remaining.access_token).await.is_ok());
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM client_sessions_v1 WHERE token_hash=?1",
            [token_digest(&login.access_token)],
            |row| row.get::<_, i64>(0)
        )
        .unwrap(),
        0
    );
}

// 旧会话升级后可重连，后续平台重启不会重置失联计时。
#[tokio::test]
async fn legacy_session_receives_one_reconnect_window_on_upgrade() {
    let (directory, _store, auth) = fixture().await;
    let login = auth
        .login("persistent-user", "field-password-123")
        .await
        .unwrap();
    let path = directory.path().join("accounts.sqlite3");
    let db = rusqlite::Connection::open(&path).unwrap();
    db.execute_batch(
        "CREATE TABLE client_sessions_legacy (
            token_hash TEXT PRIMARY KEY NOT NULL CHECK(length(token_hash)=64),
            account_id TEXT NOT NULL,
            auth_version INTEGER NOT NULL CHECK(auth_version>0),
            issued_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL CHECK(expires_at>issued_at)
         );
         INSERT INTO client_sessions_legacy
            SELECT token_hash,account_id,auth_version,issued_at-180000,expires_at
            FROM client_sessions_v1;
         DROP TABLE client_sessions_v1;
         ALTER TABLE client_sessions_legacy RENAME TO client_sessions_v1;",
    )
    .unwrap();
    let upgraded = ClientAuth::new(AccountStore::open(path.clone()).await.unwrap(), 28_800);
    assert!(upgraded.authenticate(&login.access_token).await.is_ok());

    db.execute(
        "UPDATE client_sessions_v1 SET last_seen_at=?1",
        [chrono::Utc::now().timestamp_millis() - 121_000],
    )
    .unwrap();
    let restarted = ClientAuth::new(AccountStore::open(path).await.unwrap(), 28_800);
    assert!(matches!(
        restarted.authenticate(&login.access_token).await,
        Err(ClientAuthError::Unauthorized)
    ));
}

// 已有账号库首次启用会话辅助表时，不改变账号库版本或原有账号数据。
#[tokio::test]
async fn adding_session_storage_preserves_account_schema_version() {
    let (directory, store, _auth) = fixture().await;
    let db = rusqlite::Connection::open(directory.path().join("accounts.sqlite3")).unwrap();
    let before: i64 = db
        .pragma_query_value(None, "user_version", |r| r.get(0))
        .unwrap();
    let accounts_before = store.list().await.unwrap();
    db.execute_batch("DROP TABLE client_sessions_v1").unwrap();
    let reopened = AccountStore::open(directory.path().join("accounts.sqlite3"))
        .await
        .unwrap();
    let after: i64 = db
        .pragma_query_value(None, "user_version", |r| r.get(0))
        .unwrap();
    assert_eq!(before, after);
    assert_eq!(reopened.list().await.unwrap()[0].id, accounts_before[0].id);
    assert!(
        ClientAuth::new(reopened, 28_800)
            .login("persistent-user", "field-password-123")
            .await
            .is_ok()
    );
}

// 写入会话后的登录时间更新失败，必须回滚会话插入，避免留下不可用的登录记录。
#[tokio::test]
async fn failed_login_timestamp_rolls_back_session_insert() {
    let (directory, store, auth) = fixture().await;
    let db = rusqlite::Connection::open(directory.path().join("accounts.sqlite3")).unwrap();
    db.execute_batch("CREATE TRIGGER reject_login_time BEFORE UPDATE OF last_login_at ON accounts BEGIN SELECT RAISE(FAIL,'测试故障'); END;").unwrap();
    assert!(matches!(
        auth.login("persistent-user", "field-password-123").await,
        Err(ClientAuthError::Storage)
    ));
    assert_eq!(
        db.query_row("SELECT count(*) FROM client_sessions_v1", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        0
    );
    assert!(store.list().await.unwrap()[0].last_login_at.is_none());
    db.execute_batch("DROP TRIGGER reject_login_time").unwrap();
    assert!(
        auth.login("persistent-user", "field-password-123")
            .await
            .is_ok()
    );
}

// 已存在但缺少字段的会话表拒绝启动，不能静默重建并丢失登录状态。
#[tokio::test]
async fn malformed_session_schema_is_rejected_without_repair() {
    let (directory, _store, _auth) = fixture().await;
    let db = rusqlite::Connection::open(directory.path().join("accounts.sqlite3")).unwrap();
    db.execute_batch(
        "DROP TABLE client_sessions_v1; CREATE TABLE client_sessions_v1(token_hash TEXT);",
    )
    .unwrap();
    assert!(matches!(
        AccountStore::open(directory.path().join("accounts.sqlite3")).await,
        Err(AccountError::Storage)
    ));
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM pragma_table_info('client_sessions_v1')",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        1
    );
}
