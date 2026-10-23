use super::*;
use crate::accounts::{AccountStore, NewAccount};
use proto::template_catalog_client::TemplateCatalogClient;
use tokio_stream::wrappers::TcpListenerStream;

fn sample() -> serde_json::Value {
    serde_json::json!({"schema_version":1,"id":"demo","name":"demo","manufacturer":"","models":[],"protocol_version":"","address_base":0,"notes":[],"sources":[],"points":[{"id":"stable-p1","tag":"V","group":"","unit":"V","function_code":3,"address":0,"data_type":"UINT16","reg_count":1,"scale":"1","offset":"0","deadband":"0","word_order":"HL","byte_order":"AB","bit_index":null,"source_page":0,"description":""}]})
}
fn request<T>(body: T, token: &str) -> Request<T> {
    let mut request = Request::new(body);
    request
        .metadata_mut()
        .insert("authorization", format!("Bearer {token}").parse().unwrap());
    request
}

#[tokio::test]
async fn real_rpc_requires_live_bearer_and_reads_current_and_history() {
    let dir = tempfile::tempdir().unwrap();
    let accounts = AccountStore::open(dir.path().join("accounts.sqlite3"))
        .await
        .unwrap();
    for role in ["operator", "admin"] {
        accounts
            .create(NewAccount {
                role: role.into(),
                username: role.into(),
                display_name: role.into(),
                password: "test-password-123".into(),
            })
            .await
            .unwrap();
    }
    let auth = ClientAuth::new(accounts, 28800);
    let store = TemplateStore::new(dir.path().join("templates")).unwrap();
    let first = store.create(sample()).await.unwrap();
    let id = first["id"].as_str().unwrap();
    let mut changed = sample();
    changed["name"] = serde_json::json!("new version");
    store.update(id, 1, changed).await.unwrap();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel();
    let service = TemplateCatalogService::new(store.clone(), auth.clone());
    let task = tokio::spawn(async move {
        tonic::transport::Server::builder()
            .add_service(service.server())
            .serve_with_incoming_shutdown(TcpListenerStream::new(listener), async {
                let _ = shutdown_rx.await;
            })
            .await
            .unwrap();
    });
    let mut client = TemplateCatalogClient::connect(format!("http://{address}"))
        .await
        .unwrap()
        .max_decoding_message_size(MAX_GRPC_BYTES);
    assert_eq!(
        client
            .list_templates(proto::ListTemplatesRequest {})
            .await
            .unwrap_err()
            .code(),
        tonic::Code::Unauthenticated
    );
    let mut cookie = Request::new(proto::ListTemplatesRequest {});
    cookie
        .metadata_mut()
        .insert("cookie", "mskdsp_session=web-admin".parse().unwrap());
    assert_eq!(
        client.list_templates(cookie).await.unwrap_err().code(),
        tonic::Code::Unauthenticated
    );
    for role in ["operator", "admin"] {
        let login = auth.login(role, "test-password-123").await.unwrap();
        let token = &login.access_token;
        let list = client
            .list_templates(request(proto::ListTemplatesRequest {}, token))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(list.templates.len(), 1);
        assert_eq!(list.templates[0].revision, "2");
        assert_eq!(list.templates[0].name, "new version");
        let historical = client
            .get_template(request(
                proto::GetTemplateRequest {
                    id: id.into(),
                    revision: "1".into(),
                },
                token,
            ))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(historical.revision, "1");
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&historical.template_json).unwrap(),
            sample()
        );
        let mut duplicate = request(proto::ListTemplatesRequest {}, token);
        duplicate
            .metadata_mut()
            .append("authorization", format!("Bearer {token}").parse().unwrap());
        assert_eq!(
            client.list_templates(duplicate).await.unwrap_err().code(),
            tonic::Code::Unauthenticated
        );
        assert_eq!(
            client
                .get_template(request(
                    proto::GetTemplateRequest {
                        id: id.into(),
                        revision: "01".into()
                    },
                    token
                ))
                .await
                .unwrap_err()
                .code(),
            tonic::Code::InvalidArgument
        );
        auth.logout(token).await.unwrap();
        assert_eq!(
            client
                .list_templates(request(proto::ListTemplatesRequest {}, token))
                .await
                .unwrap_err()
                .code(),
            tonic::Code::Unauthenticated
        );
    }
    let login = auth.login("operator", "test-password-123").await.unwrap();
    let token = &login.access_token;
    let mut large = sample();
    let point = large["points"][0].clone();
    large["points"] = serde_json::Value::Array(
        (0..1300)
            .map(|index| {
                let mut value = point.clone();
                value["id"] = serde_json::json!(format!("point-{index}"));
                value["tag"] = serde_json::json!(format!("tag-{index}"));
                value["description"] = serde_json::json!("x".repeat(3800));
                value
            })
            .collect(),
    );
    let large_record = store.create(large).await.unwrap();
    let large_id = large_record["id"].as_str().unwrap();
    let large_response = client
        .get_template(request(
            proto::GetTemplateRequest {
                id: large_id.into(),
                revision: String::new(),
            },
            token,
        ))
        .await
        .unwrap()
        .into_inner();
    assert!(
        large_response.template_json.len() > 4 * 1024 * 1024,
        "exercise tonic default receive-limit override"
    );
    assert!(large_response.template_json.len() <= 10 * 1024 * 1024);
    store.delete(large_id, 1).await.unwrap();
    assert_eq!(
        client
            .get_template(request(
                proto::GetTemplateRequest {
                    id: id.into(),
                    revision: u64::MAX.to_string()
                },
                token
            ))
            .await
            .unwrap_err()
            .code(),
        tonic::Code::NotFound
    );
    store.delete(id, 2).await.unwrap();
    assert!(
        client
            .list_templates(request(proto::ListTemplatesRequest {}, token))
            .await
            .unwrap()
            .into_inner()
            .templates
            .is_empty()
    );
    assert_eq!(
        client
            .get_template(request(
                proto::GetTemplateRequest {
                    id: id.into(),
                    revision: "".into()
                },
                token
            ))
            .await
            .unwrap_err()
            .code(),
        tonic::Code::NotFound
    );
    assert_eq!(
        client
            .get_template(request(
                proto::GetTemplateRequest {
                    id: id.into(),
                    revision: "2".into()
                },
                token
            ))
            .await
            .unwrap()
            .into_inner()
            .revision,
        "2"
    );
    assert!(auth.expire_session_for_test(token).await);
    assert_eq!(
        client
            .get_template(request(
                proto::GetTemplateRequest {
                    id: id.into(),
                    revision: "2".into()
                },
                token
            ))
            .await
            .unwrap_err()
            .code(),
        tonic::Code::Unauthenticated
    );
    let login = auth.login("admin", "test-password-123").await.unwrap();
    auth.revoke_account(&login.account.id).await;
    assert_eq!(
        client
            .list_templates(request(proto::ListTemplatesRequest {}, &login.access_token))
            .await
            .unwrap_err()
            .code(),
        tonic::Code::Unauthenticated
    );
    let _ = shutdown_tx.send(());
    task.await.unwrap();
}

#[test]
fn summary_preserves_full_u64_revision_and_rejects_corrupt_metadata() {
    let mut value = serde_json::json!({"id":uuid::Uuid::new_v4().to_string(),"revision":u64::MAX,"name":"test","manufacturer":"","models":[],"protocol_version":"","point_count":1,"updated_at":u64::MAX});
    assert_eq!(
        summary(value.clone()).unwrap().revision,
        u64::MAX.to_string()
    );
    value["models"] = serde_json::json!(["x".repeat(257)]);
    assert_eq!(summary(value).unwrap_err(), "点表目录记录损坏");
}
