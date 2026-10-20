use super::*;
use crate::{
    auth::{AdminAuth, AuthConfig, hash_password},
    state::SessionRegistry,
};
use axum::{
    body::{Body, to_bytes},
    http::Request,
};
use tower::ServiceExt;

async fn fixture() -> (
    tempfile::TempDir,
    IdentityStore,
    Router,
    String,
    SessionRegistry,
) {
    let directory = tempfile::tempdir().unwrap();
    let store = IdentityStore::open(directory.path().join("identity.db"))
        .await
        .unwrap();
    let auth = AdminAuth::new(AuthConfig {
        password_hash: hash_password("identity-test-password").unwrap(),
        ..AuthConfig::default()
    })
    .unwrap();
    let state = HttpState {
        auth,
        sessions: SessionRegistry::default(),
        protocol_version: "platform.v1".into(),
    };
    let sessions = state.sessions.clone();
    let app = crate::http::router(state.clone()).merge(router(state, store.clone()));
    let response = request(
        &app,
        "POST",
        "/api/v1/auth/login",
        "",
        "auth",
        "",
        json!({"username":"root","password":"identity-test-password"}),
    )
    .await;
    assert_eq!(response.status(), StatusCode::OK);
    let cookie = response.headers()[header::SET_COOKIE]
        .to_str()
        .unwrap()
        .split(';')
        .next()
        .unwrap()
        .to_string();
    (directory, store, app, cookie, sessions)
}

async fn request(
    app: &Router,
    method: &str,
    path: &str,
    cookie: &str,
    action: &str,
    origin: &str,
    value: serde_json::Value,
) -> Response {
    let mut request = Request::builder()
        .method(method)
        .uri(path)
        .header(header::HOST, "platform.example")
        .header(header::CONTENT_TYPE, "application/json");
    if !cookie.is_empty() {
        request = request.header(header::COOKIE, cookie);
    }
    if !action.is_empty() {
        request = request.header("X-MskDSP-Action", action);
    }
    if !origin.is_empty() {
        request = request.header(header::ORIGIN, origin);
    }
    app.clone()
        .oneshot(request.body(Body::from(value.to_string())).unwrap())
        .await
        .unwrap()
}

async fn json(response: Response) -> serde_json::Value {
    serde_json::from_slice(&to_bytes(response.into_body(), 1024 * 1024).await.unwrap()).unwrap()
}

// 未登录不能管理身份，页面跳转登录，写接口拒绝跨源和缺失操作标记。
#[tokio::test]
async fn admin_and_origin_are_required() {
    let (_directory, _store, app, cookie, _sessions) = fixture().await;
    let response = request(&app, "GET", "/agents", "", "", "", json!(null)).await;
    assert_eq!(response.status(), StatusCode::SEE_OTHER);
    for path in [
        "/api/v1/agents",
        "/api/v1/agents/unknown",
        "/api/v1/agents/unknown/events",
    ] {
        assert_eq!(
            request(&app, "GET", path, "", "", "", json!(null))
                .await
                .status(),
            StatusCode::UNAUTHORIZED
        );
    }
    let input = json!({"name":"测试上位机","site_id":"test-site","notes":""});
    for (action, origin) in [
        ("", ""),
        ("agent-identity", "https://other.example"),
        ("agent-identity", "null"),
    ] {
        assert_eq!(
            request(
                &app,
                "POST",
                "/api/v1/agents",
                &cookie,
                action,
                origin,
                input.clone()
            )
            .await
            .status(),
            StatusCode::FORBIDDEN
        );
    }
    assert_eq!(
        request(&app, "GET", "/agents", &cookie, "", "", json!(null))
            .await
            .status(),
        StatusCode::OK
    );
}

// 管理闭环返回一次性凭据，普通详情无令牌，版本冲突不覆盖，撤销和解绑真正改变接入权限。
#[tokio::test]
async fn management_lifecycle_and_conflict() {
    let (_directory, store, app, cookie, _sessions) = fixture().await;
    let response = request(
        &app,
        "POST",
        "/api/v1/agents",
        &cookie,
        "agent-identity",
        "https://platform.example",
        json!({"name":"测试上位机","site_id":"site-1","notes":""}),
    )
    .await;
    assert_eq!(response.status(), StatusCode::CREATED);
    assert_eq!(response.headers()[header::CACHE_CONTROL], "no-store");
    let issued = json(response).await;
    let id = issued["agent"]["id"].as_str().unwrap();
    let token = issued["token"].as_str().unwrap();
    assert!(store.authenticate(id, token, "site-1", "v1").await.is_ok());
    let path = format!("/api/v1/agents/{id}");
    let detail = json(request(&app, "GET", &path, &cookie, "", "", json!(null)).await).await;
    assert_eq!(detail["online"], false);
    assert!(!detail.to_string().contains(token));
    assert!(detail.get("token_digest").is_none());
    let update =
        json!({"revision":1,"name":"修改后","site_id":"site-1","notes":"备注","enabled":false});
    assert_eq!(
        request(
            &app,
            "PUT",
            &path,
            &cookie,
            "agent-identity",
            "",
            update.clone()
        )
        .await
        .status(),
        StatusCode::OK
    );
    assert!(store.authenticate(id, token, "site-1", "v1").await.is_err());
    assert_eq!(
        request(&app, "PUT", &path, &cookie, "agent-identity", "", update)
            .await
            .status(),
        StatusCode::CONFLICT
    );
    let rotated = json(
        request(
            &app,
            "POST",
            &format!("{path}/rotate"),
            &cookie,
            "agent-identity",
            "",
            json!({"revision":2}),
        )
        .await,
    )
    .await;
    assert_ne!(rotated["token"], issued["token"]);
    let revoked = json(
        request(
            &app,
            "POST",
            &format!("{path}/revoke"),
            &cookie,
            "agent-identity",
            "",
            json!({"revision":3}),
        )
        .await,
    )
    .await;
    assert_eq!(revoked["credential_active"], false);
    let unbound = json(
        request(
            &app,
            "POST",
            &format!("{path}/unbind"),
            &cookie,
            "agent-identity",
            "",
            json!({"revision":4}),
        )
        .await,
    )
    .await;
    assert_eq!(unbound["site_id"], "");
    assert_eq!(unbound["enabled"], false);
    let events = json(
        request(
            &app,
            "GET",
            &format!("{path}/events"),
            &cookie,
            "",
            "",
            json!(null),
        )
        .await,
    )
    .await;
    assert_eq!(events.as_array().unwrap().len(), 5);
    assert_eq!(events[0]["actor"], "root");
    assert_eq!(events[0]["action"], "unbound");
}

// 非法字段、未知身份、超大 JSON 和伪造字段被拒绝且不产生数据。
#[tokio::test]
async fn input_boundaries_and_unknown_records() {
    let (_directory, store, app, cookie, _sessions) = fixture().await;
    for input in [
        json!({"name":""}),
        json!({"name":"test","site_id":"x\u{0000}"}),
    ] {
        assert_eq!(
            request(
                &app,
                "POST",
                "/api/v1/agents",
                &cookie,
                "agent-identity",
                "",
                input
            )
            .await
            .status(),
            StatusCode::BAD_REQUEST
        );
    }
    assert_eq!(
        request(
            &app,
            "POST",
            "/api/v1/agents",
            &cookie,
            "agent-identity",
            "",
            json!({"name":"test","token":"injected"})
        )
        .await
        .status(),
        StatusCode::UNPROCESSABLE_ENTITY
    );
    assert_eq!(
        request(
            &app,
            "POST",
            "/api/v1/agents",
            &cookie,
            "agent-identity",
            "",
            json!({"name":"test","notes":"x".repeat(17000)})
        )
        .await
        .status(),
        StatusCode::PAYLOAD_TOO_LARGE
    );
    assert_eq!(
        request(
            &app,
            "GET",
            "/api/v1/agents/unknown",
            &cookie,
            "",
            "",
            json!(null)
        )
        .await
        .status(),
        StatusCode::NOT_FOUND
    );
    assert!(store.list().await.unwrap().is_empty());
}

// 管理接口显示真实会话，管理员禁用后立即关闭 gRPC，探针与列表均转为离线。
#[tokio::test]
async fn web_disable_closes_real_grpc_connection() {
    use crate::grpc::{
        AgentGrpcService, AgentServiceServer,
        agent::{self, agent_message, server_message},
    };
    use tokio::{
        net::TcpListener,
        sync::mpsc,
        time::{Duration, timeout},
    };
    use tokio_stream::wrappers::{ReceiverStream, TcpListenerStream};
    let (_directory, store, app, cookie, sessions) = fixture().await;
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let service = AgentGrpcService::new(sessions.clone(), "platform.v1".into(), store.clone());
    let server = tokio::spawn(async move {
        tonic::transport::Server::builder()
            .add_service(AgentServiceServer::new(service))
            .serve_with_incoming(TcpListenerStream::new(listener))
            .await
            .unwrap();
    });
    let issued = store
        .register(
            NewAgent {
                name: "联通验证".into(),
                site_id: "site-1".into(),
                notes: String::new(),
            },
            "root",
        )
        .await
        .unwrap();
    let mut client =
        agent::agent_service_client::AgentServiceClient::connect(format!("http://{address}"))
            .await
            .unwrap();
    let (sender, receiver) = mpsc::channel(2);
    sender
        .send(agent::AgentMessage {
            payload: Some(agent_message::Payload::Hello(agent::Hello {
                agent_id: issued.agent.id.clone(),
                site_id: "site-1".into(),
                agent_version: "test-v1".into(),
                protocol_version: "platform.v1".into(),
                capabilities: vec![],
            })),
        })
        .await
        .unwrap();
    let mut connect = tonic::Request::new(ReceiverStream::new(receiver));
    connect.metadata_mut().insert(
        "authorization",
        format!("Bearer {}", issued.token).parse().unwrap(),
    );
    let mut inbound = client.connect_agent(connect).await.unwrap().into_inner();
    assert!(matches!(
        inbound.message().await.unwrap().unwrap().payload,
        Some(server_message::Payload::Welcome(_))
    ));
    let path = format!("/api/v1/agents/{}", issued.agent.id);
    let online = json(request(&app, "GET", &path, &cookie, "", "", json!(null)).await).await;
    assert_eq!(online["online"], true);
    assert_eq!(online["agent_version"], "test-v1");
    let response = request(
        &app,
        "PUT",
        &path,
        &cookie,
        "agent-identity",
        "",
        json!({"revision":1,"name":"联通验证","site_id":"site-1","notes":"","enabled":false}),
    )
    .await;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(json(response).await["online"], false);
    let error = timeout(Duration::from_secs(2), inbound.message())
        .await
        .unwrap()
        .unwrap_err();
    assert_eq!(error.code(), tonic::Code::PermissionDenied);
    assert_eq!(sessions.count().await, 0);
    server.abort();
    let _ = server.await;
}
