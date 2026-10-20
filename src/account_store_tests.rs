use super::*;

async fn fixture() -> (tempfile::TempDir, AccountStore) {
    let directory = tempfile::tempdir().unwrap();
    let store = AccountStore::open(directory.path().join("accounts/accounts.sqlite3"))
        .await
        .unwrap();
    (directory, store)
}

fn account(username: &str) -> NewAccount {
    NewAccount {
        username: username.into(),
        password: "test-password".into(),
        display_name: String::new(),
    }
}

#[tokio::test]
async fn persists_normalized_account_without_password_in_response() {
    let (directory, store) = fixture().await;
    let created = store.create(account("  Field_01  ")).await.unwrap();
    assert_eq!(created.username, "field_01");
    assert_eq!(created.display_name, "field_01");
    assert!(created.enabled);
    assert_eq!(created.revision, 1);
    assert_eq!(created.auth_version, 1);
    assert_eq!(created.last_login_at, None);
    assert!(created.created_at > 1_000_000_000_000);
    assert_eq!(created.created_at, created.updated_at);
    let serialized = serde_json::to_string(&created).unwrap();
    assert!(!serialized.contains("password"));
    assert!(!serialized.contains("auth_version"));
    let path = directory.path().join("accounts/accounts.sqlite3");
    assert!(path.exists());
    let connection = Connection::open(path).unwrap();
    let hash: String = connection
        .query_row("SELECT password_hash FROM accounts", [], |r| r.get(0))
        .unwrap();
    assert!(hash.starts_with("$argon2id$"));
    assert_ne!(hash, "test-password");
    drop(store);
    let reopened = AccountStore::open(directory.path().join("accounts/accounts.sqlite3"))
        .await
        .unwrap();
    assert_eq!(reopened.list().await.unwrap()[0].id, created.id);
    assert_eq!(
        reopened
            .verify("FIELD_01", "test-password")
            .await
            .unwrap()
            .id,
        created.id
    );
}

#[tokio::test]
async fn rejects_invalid_usernames_passwords_and_display_names() {
    let (_directory, store) = fixture().await;
    for name in [
        "ab",
        "中文账户",
        "with space",
        "a/b",
        "a@b",
        &"a".repeat(65),
    ] {
        assert!(matches!(
            store.create(account(name)).await,
            Err(AccountError::Invalid(_))
        ));
    }
    for password in ["short", "        ", &"a".repeat(129)] {
        let mut new = account("valid-user");
        new.password = password.into();
        assert!(matches!(
            store.create(new).await,
            Err(AccountError::Invalid(_))
        ));
    }
    let mut new = account("valid-user");
    new.display_name = "界".repeat(43);
    assert!(matches!(
        store.create(new).await,
        Err(AccountError::Invalid(_))
    ));
    for display_name in ["现场\n一", "现场\0一", "现场\t一", "现场\u{7f}一"] {
        let mut new = account("valid-user");
        new.display_name = display_name.into();
        assert!(matches!(
            store.create(new).await,
            Err(AccountError::Invalid(_))
        ));
    }
    assert!(store.list().await.unwrap().is_empty());
}

#[tokio::test]
async fn accepts_exact_field_boundaries_and_preserves_password_spaces() {
    let (_directory, store) = fixture().await;
    store
        .create(NewAccount {
            username: "abc".into(),
            password: " 123456 ".into(),
            display_name: "名".into(),
        })
        .await
        .unwrap();
    store.verify("abc", " 123456 ").await.unwrap();
    assert!(matches!(
        store.verify("abc", "123456").await,
        Err(AccountError::Unauthorized)
    ));
    let display_name = format!("{}aa", "界".repeat(42));
    let created = store
        .create(NewAccount {
            username: "a".repeat(64),
            password: "密".repeat(42) + "aa",
            display_name: display_name.clone(),
        })
        .await
        .unwrap();
    assert_eq!(created.display_name, display_name);
    assert_eq!(created.username.len(), 64);
}

#[tokio::test]
async fn usernames_are_unique_after_normalization() {
    let (_directory, store) = fixture().await;
    store.create(account("Field-01")).await.unwrap();
    assert!(matches!(
        store.create(account(" FIELD-01 ")).await,
        Err(AccountError::DuplicateUsername)
    ));
}

#[tokio::test]
async fn revision_guards_edits_and_auth_version_invalidates_old_sessions() {
    let (_directory, store) = fixture().await;
    let created = store.create(account("field-01")).await.unwrap();
    let renamed = store
        .update(
            &created.id,
            1,
            AccountUpdate {
                display_name: "  现场一  ".into(),
                enabled: true,
            },
        )
        .await
        .unwrap();
    assert_eq!(renamed.display_name, "现场一");
    assert_eq!(renamed.revision, 2);
    assert_eq!(renamed.auth_version, 1);
    assert!(matches!(
        store
            .update(
                &created.id,
                1,
                AccountUpdate {
                    display_name: "过期修改".into(),
                    enabled: false
                }
            )
            .await,
        Err(AccountError::Conflict)
    ));
    let disabled = store
        .update(
            &created.id,
            2,
            AccountUpdate {
                display_name: "现场一".into(),
                enabled: false,
            },
        )
        .await
        .unwrap();
    assert_eq!(disabled.auth_version, 2);
    assert!(matches!(
        store.check_authorization(&created.id, 1).await,
        Err(AccountError::Unauthorized)
    ));
    assert!(matches!(
        store.verify("field-01", "test-password").await,
        Err(AccountError::Unauthorized)
    ));
    let enabled = store
        .update(
            &created.id,
            3,
            AccountUpdate {
                display_name: "现场一".into(),
                enabled: true,
            },
        )
        .await
        .unwrap();
    assert_eq!(enabled.auth_version, 3);
    assert!(matches!(
        store.check_authorization(&created.id, 2).await,
        Err(AccountError::Unauthorized)
    ));
    store.check_authorization(&created.id, 3).await.unwrap();
}

#[tokio::test]
async fn password_reset_is_revision_guarded_and_revokes_old_authorization() {
    let (_directory, store) = fixture().await;
    let created = store.create(account("field-01")).await.unwrap();
    assert!(matches!(
        store
            .reset_password(&created.id, 99, "new-password".into())
            .await,
        Err(AccountError::Conflict)
    ));
    assert!(matches!(
        store.reset_password(&created.id, 1, "short".into()).await,
        Err(AccountError::Invalid(_))
    ));
    let reset = store
        .reset_password(&created.id, 1, "new-password".into())
        .await
        .unwrap();
    assert_eq!(reset.revision, 2);
    assert_eq!(reset.auth_version, 2);
    assert!(matches!(
        store.verify("field-01", "test-password").await,
        Err(AccountError::Unauthorized)
    ));
    store.verify("field-01", "new-password").await.unwrap();
    assert!(matches!(
        store.touch_login(&created.id, 1).await,
        Err(AccountError::Unauthorized)
    ));
}

#[tokio::test]
async fn login_timestamp_does_not_change_management_revision() {
    let (_directory, store) = fixture().await;
    let created = store.create(account("field-01")).await.unwrap();
    assert!(matches!(
        store.verify("missing", "test-password").await,
        Err(AccountError::Unauthorized)
    ));
    assert!(matches!(
        store.verify("field-01", "wrong-password").await,
        Err(AccountError::Unauthorized)
    ));
    store.verify("field-01", "test-password").await.unwrap();
    assert_eq!(store.get(&created.id).await.unwrap().last_login_at, None);
    let touched = store
        .touch_login(&created.id, created.auth_version)
        .await
        .unwrap();
    assert!(touched.last_login_at.is_some());
    assert!(touched.last_login_at.unwrap() > 1_000_000_000_000);
    assert_eq!(touched.revision, created.revision);
    assert_eq!(touched.updated_at, created.updated_at);
    assert!(matches!(
        store.get("unknown").await,
        Err(AccountError::NotFound)
    ));
    assert!(matches!(
        store.check_authorization("unknown", 1).await,
        Err(AccountError::Unauthorized)
    ));
}

#[tokio::test]
async fn refuses_unknown_version_corruption_and_missing_schema() {
    for scenario in ["version", "corrupt", "schema"] {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("accounts/accounts.sqlite3");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        if scenario == "corrupt" {
            std::fs::write(&path, b"not sqlite").unwrap();
        } else {
            let connection = Connection::open(&path).unwrap();
            connection
                .pragma_update(
                    None,
                    "user_version",
                    if scenario == "version" { 999 } else { 1 },
                )
                .unwrap();
        }
        assert!(matches!(
            AccountStore::open(directory.path().join("accounts/accounts.sqlite3")).await,
            Err(AccountError::Storage)
        ));
    }
}

#[tokio::test]
async fn refuses_directory_database_path() {
    let directory = tempfile::tempdir().unwrap();
    assert!(matches!(
        AccountStore::open(directory.path().to_path_buf()).await,
        Err(AccountError::Storage)
    ));
}

#[cfg(unix)]
#[tokio::test]
async fn refuses_symbolic_link_database_path() {
    let directory = tempfile::tempdir().unwrap();
    let target = directory.path().join("target.sqlite3");
    std::fs::write(&target, b"").unwrap();
    let link = directory.path().join("link.sqlite3");
    std::os::unix::fs::symlink(target, &link).unwrap();
    assert!(matches!(
        AccountStore::open(link).await,
        Err(AccountError::Storage)
    ));
}

#[tokio::test]
async fn concurrent_edits_only_accept_one_revision() {
    let (_directory, store) = fixture().await;
    let created = store.create(account("field-01")).await.unwrap();
    let (one, two) = tokio::join!(
        store.update(
            &created.id,
            1,
            AccountUpdate {
                display_name: "甲".into(),
                enabled: true
            }
        ),
        store.update(
            &created.id,
            1,
            AccountUpdate {
                display_name: "乙".into(),
                enabled: true
            }
        )
    );
    assert!(matches!(
        (&one, &two),
        (Ok(_), Err(AccountError::Conflict)) | (Err(AccountError::Conflict), Ok(_))
    ));
}

#[tokio::test]
async fn enforces_account_capacity_without_replacing_existing_accounts() {
    let (directory, store) = fixture().await;
    let created = store.create(account("field-01")).await.unwrap();
    let mut connection =
        Connection::open(directory.path().join("accounts/accounts.sqlite3")).unwrap();
    let transaction = connection.transaction().unwrap();
    transaction.execute("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<9999) INSERT INTO accounts (id,username,display_name,password_hash,enabled,revision,auth_version,created_at,updated_at) SELECT 'id-'||i, 'user-'||i, '用户', (SELECT password_hash FROM accounts LIMIT 1), 1,1,1,1,1 FROM n", []).unwrap();
    transaction.commit().unwrap();
    assert!(matches!(
        store.create(account("extra-account")).await,
        Err(AccountError::Invalid(_))
    ));
    assert_eq!(store.get(&created.id).await.unwrap().username, "field-01");
    assert_eq!(store.list().await.unwrap().len(), 10000);
}
