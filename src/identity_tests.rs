use super::*;

fn registration() -> NewAgent {
    NewAgent {
        name: "现场调试电脑".into(),
        site_id: "station-01".into(),
        notes: "首期登记".into(),
    }
}

// 登记只返回一次明文令牌，数据库和普通详情无明文，重启保留身份。
#[tokio::test]
async fn registration_persists_without_plaintext_token() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("identities.sqlite3");
    let store = IdentityStore::open(path.clone()).await.unwrap();
    let issued = store.register(registration(), "root").await.unwrap();
    assert_eq!(issued.token.len(), 64);
    let id = issued.agent.id.clone();
    assert!(
        !serde_json::to_string(&issued.agent)
            .unwrap()
            .contains(&issued.token)
    );
    let authenticated = store
        .authenticate(&id, &issued.token, "station-01", "0.7.0")
        .await
        .unwrap();
    assert_eq!(authenticated.site_id, "station-01");
    assert!(store.get(&id).await.unwrap().last_seen_at.is_none());
    store
        .connected(&id, authenticated.credential_version, "0.7.0")
        .await
        .unwrap();
    drop(store);
    let bytes = std::fs::read(&path).unwrap();
    assert!(
        !bytes
            .windows(issued.token.len())
            .any(|part| part == issued.token.as_bytes())
    );
    let store = IdentityStore::open(path).await.unwrap();
    let record = store.get(&id).await.unwrap();
    assert_eq!(record.agent_version, "0.7.0");
    assert!(record.last_seen_at.is_some());
    assert_eq!(store.list().await.unwrap().len(), 1);
    assert_eq!(store.events(&id).await.unwrap()[0].action, "registered");
    assert!(
        store
            .authenticate(&id, &issued.token, "station-01", "0.7.0")
            .await
            .is_ok()
    );
}

// 无效令牌、未知身份、错误现场都不能建立接入身份。
#[tokio::test]
async fn authentication_rejects_forged_identity_and_site() {
    let directory = tempfile::tempdir().unwrap();
    let store = IdentityStore::open(directory.path().join("identity.db"))
        .await
        .unwrap();
    let issued = store.register(registration(), "root").await.unwrap();
    for (id, token, site) in [
        (issued.agent.id.as_str(), "wrong", "station-01"),
        ("unknown", issued.token.as_str(), "station-01"),
        (
            issued.agent.id.as_str(),
            issued.token.as_str(),
            "forged-site",
        ),
    ] {
        assert!(matches!(
            store.authenticate(id, token, site, "v1").await,
            Err(IdentityError::Unauthorized)
        ));
    }
    assert!(
        store
            .get(&issued.agent.id)
            .await
            .unwrap()
            .last_seen_at
            .is_none()
    );
}

// 更新版本阻止覆盖；禁用、轮换、撤销、解绑使既有接入凭据或会话失效。
#[tokio::test]
async fn lifecycle_revisions_and_audit_are_atomic() {
    let directory = tempfile::tempdir().unwrap();
    let store = IdentityStore::open(directory.path().join("identity.db"))
        .await
        .unwrap();
    let issued = store.register(registration(), "root").await.unwrap();
    let id = &issued.agent.id;
    let session = store
        .authenticate(id, &issued.token, "station-01", "v1")
        .await
        .unwrap();
    let disabled = store
        .update(
            id,
            1,
            AgentUpdate {
                name: "现场电脑".into(),
                site_id: "station-01".into(),
                notes: String::new(),
                enabled: false,
            },
            "root",
        )
        .await
        .unwrap();
    assert!(
        store
            .check_session(id, session.credential_version)
            .await
            .is_err()
    );
    assert!(
        store
            .authenticate(id, &issued.token, "station-01", "v1")
            .await
            .is_err()
    );
    assert!(matches!(
        store.revoke(id, 1, "root").await,
        Err(IdentityError::Conflict)
    ));
    assert_eq!(store.events(id).await.unwrap().len(), 2);
    let enabled = store
        .update(
            id,
            disabled.revision,
            AgentUpdate {
                name: "现场电脑".into(),
                site_id: "station-01".into(),
                notes: String::new(),
                enabled: true,
            },
            "root",
        )
        .await
        .unwrap();
    let rotated = store.rotate(id, enabled.revision, "root").await.unwrap();
    assert_ne!(issued.token, rotated.token);
    assert!(
        store
            .authenticate(id, &issued.token, "station-01", "v1")
            .await
            .is_err()
    );
    let session = store
        .authenticate(id, &rotated.token, "station-01", "v1")
        .await
        .unwrap();
    let revoked = store
        .revoke(id, rotated.agent.revision, "root")
        .await
        .unwrap();
    assert!(!revoked.credential_active);
    assert!(
        store
            .check_session(id, session.credential_version)
            .await
            .is_err()
    );
    let rotated = store.rotate(id, revoked.revision, "root").await.unwrap();
    let unbound = store
        .unbind(id, rotated.agent.revision, "root")
        .await
        .unwrap();
    assert!(!unbound.enabled && !unbound.credential_active && unbound.site_id.is_empty());
    assert!(
        store
            .authenticate(id, &rotated.token, "", "v1")
            .await
            .is_err()
    );
    assert_eq!(store.events(id).await.unwrap()[0].action, "unbound");
}

// 字段边界在写入前拒绝，失败操作不能留下身份或审计记录。
#[tokio::test]
async fn validation_and_site_changes() {
    let directory = tempfile::tempdir().unwrap();
    let store = IdentityStore::open(directory.path().join("identity.db"))
        .await
        .unwrap();
    for name in [" ".to_string(), "x".repeat(129), "name\u{0000}".into()] {
        assert!(
            store
                .register(
                    NewAgent {
                        name,
                        ..registration()
                    },
                    "root"
                )
                .await
                .is_err()
        );
    }
    assert!(store.list().await.unwrap().is_empty());
    let issued = store.register(registration(), "root").await.unwrap();
    let session = store
        .authenticate(&issued.agent.id, &issued.token, "station-01", "v1")
        .await
        .unwrap();
    store
        .update(
            &issued.agent.id,
            1,
            AgentUpdate {
                name: "新名称".into(),
                site_id: "station-02".into(),
                notes: String::new(),
                enabled: true,
            },
            "root",
        )
        .await
        .unwrap();
    assert!(
        store
            .check_session(&issued.agent.id, session.credential_version)
            .await
            .is_err()
    );
    assert!(
        store
            .authenticate(&issued.agent.id, &issued.token, "station-01", "v1")
            .await
            .is_err()
    );
    assert!(
        store
            .authenticate(&issued.agent.id, &issued.token, "station-02", "v1")
            .await
            .is_ok()
    );
}
