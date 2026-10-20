use super::*;
use crate::accounts::{AccountUpdate, NewAccount};

async fn fixture() -> (tempfile::TempDir, AccountStore, ClientAuth, AccountRecord) {
    let directory = tempfile::tempdir().unwrap();
    let store = AccountStore::open(directory.path().join("accounts.sqlite3"))
        .await
        .unwrap();
    let account = store
        .create(NewAccount {
            username: "field-user".into(),
            display_name: "现场账号".into(),
            password: "field-password-123".into(),
        })
        .await
        .unwrap();
    let auth = ClientAuth::new(store.clone(), 28_800);
    (directory, store, auth, account)
}

#[tokio::test]
async fn login_issues_opaque_token_and_logout_is_idempotent() {
    let (_directory, _store, auth, account) = fixture().await;
    let login = auth
        .login("field-user", "field-password-123")
        .await
        .unwrap();
    assert_eq!(login.access_token.len(), 64);
    assert!(
        login
            .access_token
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit())
    );
    assert_eq!(login.token_type, "Bearer");
    assert_eq!(login.expires_in, 28_800);
    assert_eq!(login.account.id, account.id);
    assert!(login.account.last_login_at.is_some());
    assert_eq!(
        auth.authenticate(&login.access_token)
            .await
            .unwrap()
            .account
            .id,
        account.id
    );
    auth.logout(&login.access_token).await;
    auth.logout(&login.access_token).await;
    assert!(matches!(
        auth.authenticate(&login.access_token).await,
        Err(ClientAuthError::Unauthorized)
    ));
}

#[tokio::test]
async fn wrong_credentials_have_the_same_error_and_attempts_are_limited() {
    let (_directory, _store, auth, _account) = fixture().await;
    for index in 0..10 {
        let result = if index % 2 == 0 {
            auth.login("field-user", "wrong-password").await
        } else {
            auth.login("unknown-user", "field-password-123").await
        };
        assert!(matches!(result, Err(ClientAuthError::Unauthorized)));
    }
    assert!(matches!(
        auth.login("field-user", "field-password-123").await,
        Err(ClientAuthError::RateLimited)
    ));
    auth.0.attempts.lock().await.0 = Instant::now() - Duration::from_secs(61);
    assert!(auth.login("field-user", "field-password-123").await.is_ok());
}

#[tokio::test]
async fn expiry_restart_and_malformed_tokens_are_rejected() {
    let (_directory, store, auth, _account) = fixture().await;
    let login = auth
        .login("field-user", "field-password-123")
        .await
        .unwrap();
    let restarted = ClientAuth::new(store, 28_800);
    assert!(matches!(
        restarted.authenticate(&login.access_token).await,
        Err(ClientAuthError::Unauthorized)
    ));
    for token in ["", "old-identity-token", &"a".repeat(65), &"x".repeat(64)] {
        assert!(matches!(
            auth.authenticate(token).await,
            Err(ClientAuthError::Unauthorized)
        ));
    }
    auth.0
        .sessions
        .lock()
        .await
        .get_mut(&login.access_token)
        .unwrap()
        .expires_at = Instant::now();
    assert!(matches!(
        auth.authenticate(&login.access_token).await,
        Err(ClientAuthError::Unauthorized)
    ));
    assert!(auth.0.sessions.lock().await.is_empty());
}

#[tokio::test]
async fn verification_concurrency_and_session_capacity_are_bounded() {
    let (_directory, store, auth, account) = fixture().await;
    let permits = auth
        .0
        .verification_slots
        .clone()
        .acquire_many_owned(2)
        .await
        .unwrap();
    assert!(matches!(
        auth.login("field-user", "field-password-123").await,
        Err(ClientAuthError::Busy)
    ));
    drop(permits);
    let entry = SessionEntry {
        account_id: account.id.clone(),
        auth_version: account.auth_version,
        expires_at: Instant::now() + Duration::from_secs(100),
    };
    {
        let mut sessions = auth.0.sessions.lock().await;
        for index in 0..MAX_ACCOUNT_SESSIONS {
            sessions.insert(format!("{index:064x}"), entry.clone());
        }
    }
    assert!(matches!(
        auth.login("field-user", "field-password-123").await,
        Err(ClientAuthError::SessionLimit)
    ));
    assert!(
        store
            .get(&account.id)
            .await
            .unwrap()
            .last_login_at
            .is_none()
    );
    {
        let mut sessions = auth.0.sessions.lock().await;
        sessions.clear();
        for index in 0..MAX_SESSIONS {
            sessions.insert(
                format!("{index:064x}"),
                SessionEntry {
                    account_id: "other-account".into(),
                    ..entry.clone()
                },
            );
        }
    }
    assert!(matches!(
        auth.login("field-user", "field-password-123").await,
        Err(ClientAuthError::SessionLimit)
    ));
}

#[tokio::test]
async fn revoking_account_removes_all_its_tokens_without_affecting_other_accounts() {
    let (_directory, store, auth, account) = fixture().await;
    store
        .create(NewAccount {
            username: "second-user".into(),
            display_name: "其他账号".into(),
            password: "other-password-123".into(),
        })
        .await
        .unwrap();
    let first = auth
        .login("field-user", "field-password-123")
        .await
        .unwrap();
    let second = auth
        .login("field-user", "field-password-123")
        .await
        .unwrap();
    let other = auth
        .login("second-user", "other-password-123")
        .await
        .unwrap();
    auth.revoke_account(&account.id).await;
    assert!(matches!(
        auth.authenticate(&first.access_token).await,
        Err(ClientAuthError::Unauthorized)
    ));
    assert!(matches!(
        auth.authenticate(&second.access_token).await,
        Err(ClientAuthError::Unauthorized)
    ));
    assert!(auth.authenticate(&other.access_token).await.is_ok());
}

#[tokio::test]
async fn account_changes_revalidate_sessions_and_reset_allows_fresh_login() {
    let (_directory, store, auth, account) = fixture().await;
    let login = auth
        .login("field-user", "field-password-123")
        .await
        .unwrap();
    let renamed = store
        .update(
            &account.id,
            account.revision,
            AccountUpdate {
                display_name: "更新名称".into(),
                enabled: true,
            },
        )
        .await
        .unwrap();
    assert_eq!(
        auth.authenticate(&login.access_token)
            .await
            .unwrap()
            .account
            .display_name,
        "更新名称"
    );
    let disabled = store
        .update(
            &account.id,
            renamed.revision,
            AccountUpdate {
                display_name: renamed.display_name,
                enabled: false,
            },
        )
        .await
        .unwrap();
    assert!(matches!(
        auth.authenticate(&login.access_token).await,
        Err(ClientAuthError::Unauthorized)
    ));
    assert!(matches!(
        auth.login("field-user", "field-password-123").await,
        Err(ClientAuthError::Unauthorized)
    ));
    let enabled = store
        .update(
            &account.id,
            disabled.revision,
            AccountUpdate {
                display_name: disabled.display_name,
                enabled: true,
            },
        )
        .await
        .unwrap();
    assert!(matches!(
        auth.authenticate(&login.access_token).await,
        Err(ClientAuthError::Unauthorized)
    ));
    let fresh = auth
        .login("field-user", "field-password-123")
        .await
        .unwrap();
    store
        .reset_password(&account.id, enabled.revision, "changed-password-123".into())
        .await
        .unwrap();
    assert!(matches!(
        auth.authenticate(&fresh.access_token).await,
        Err(ClientAuthError::Unauthorized)
    ));
    assert!(matches!(
        auth.login("field-user", "field-password-123").await,
        Err(ClientAuthError::Unauthorized)
    ));
    let new_login = auth
        .login("field-user", "changed-password-123")
        .await
        .unwrap();
    assert!(auth.authenticate(&new_login.access_token).await.is_ok());
}

#[tokio::test]
async fn storage_failure_is_reported_without_invalidating_an_existing_session() {
    let (directory, _store, auth, _account) = fixture().await;
    let login = auth
        .login("field-user", "field-password-123")
        .await
        .unwrap();
    let connection = rusqlite::Connection::open(directory.path().join("accounts.sqlite3")).unwrap();
    connection
        .execute_batch("ALTER TABLE accounts RENAME TO unavailable_accounts")
        .unwrap();
    assert!(matches!(
        auth.authenticate(&login.access_token).await,
        Err(ClientAuthError::Storage)
    ));
    assert!(matches!(
        auth.login("field-user", "field-password-123").await,
        Err(ClientAuthError::Storage)
    ));
    connection
        .execute_batch("ALTER TABLE unavailable_accounts RENAME TO accounts")
        .unwrap();
    assert!(auth.authenticate(&login.access_token).await.is_ok());
}
