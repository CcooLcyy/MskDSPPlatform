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
    AccountStore,
    ClientAuth,
    Router,
    String,
    SessionRegistry,
) {
    let directory = tempfile::tempdir().unwrap();
    let accounts = AccountStore::open(directory.path().join("accounts.sqlite3"))
        .await
        .unwrap();
    let clients = ClientAuth::new(accounts.clone(), 28800);
    let auth = AdminAuth::new(AuthConfig {
        password_hash: hash_password("admin-test-password").unwrap(),
        ..AuthConfig::default()
    })
    .unwrap();
    let state = HttpState {
        auth,
        sessions: SessionRegistry::default(),
        protocol_version: "platform.v1".into(),
    };
    let sessions = state.sessions.clone();
    let app =
        crate::http::router(state.clone()).merge(router(state, accounts.clone(), clients.clone()));
    let login = request(
        &app,
        "POST",
        "/api/v1/auth/login",
        "",
        "",
        "auth",
        "",
        json!({"username":"root","password":"admin-test-password"}),
    )
    .await;
    assert_eq!(login.status(), StatusCode::OK);
    let cookie = login.headers()[header::SET_COOKIE]
        .to_str()
        .unwrap()
        .split(';')
        .next()
        .unwrap()
        .to_string();
    (directory, accounts, clients, app, cookie, sessions)
}

#[allow(clippy::too_many_arguments)]
async fn request(
    app: &Router,
    method: &str,
    path: &str,
    cookie: &str,
    bearer: &str,
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
    if !bearer.is_empty() {
        request = request.header(header::AUTHORIZATION, format!("Bearer {bearer}"));
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

async fn create(app: &Router, cookie: &str) -> serde_json::Value {
    let response=request(app,"POST","/api/v1/accounts",cookie,"","account","",json!({"username":"Field.User","password":"initial-password","display_name":"现场维护账号"})).await;
    assert_eq!(response.status(), StatusCode::CREATED);
    json(response).await
}

async fn login(app: &Router, username: &str, password: &str) -> Response {
    request(
        app,
        "POST",
        "/api/v1/client/auth/login",
        "",
        "",
        "client-auth",
        "",
        json!({"username":username,"password":password}),
    )
    .await
}

// 页面和管理API仅限管理员，旧实例入口跳转，跨源与缺失标记不能写入。
#[tokio::test]
async fn administrator_and_same_origin_are_required() {
    let (_directory, _accounts, _clients, app, cookie, _sessions) = fixture().await;
    assert_eq!(
        request(&app, "GET", "/accounts", "", "", "", "", json!(null))
            .await
            .status(),
        StatusCode::SEE_OTHER
    );
    let legacy = request(&app, "GET", "/agents", &cookie, "", "", "", json!(null)).await;
    assert_eq!(legacy.headers()[header::LOCATION], "/accounts");
    assert_eq!(
        request(
            &app,
            "GET",
            "/api/v1/agents",
            &cookie,
            "",
            "",
            "",
            json!(null)
        )
        .await
        .status(),
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        request(&app, "GET", "/api/v1/accounts", "", "", "", "", json!(null))
            .await
            .status(),
        StatusCode::UNAUTHORIZED
    );
    for (action, origin) in [
        ("", ""),
        ("account", "https://evil.example"),
        ("account", "null"),
    ] {
        assert_eq!(
            request(
                &app,
                "POST",
                "/api/v1/accounts",
                &cookie,
                "",
                action,
                origin,
                json!({"username":"field","password":"valid-password"})
            )
            .await
            .status(),
            StatusCode::FORBIDDEN
        );
    }
    assert_eq!(
        request(&app, "GET", "/accounts", &cookie, "", "", "", json!(null))
            .await
            .status(),
        StatusCode::OK
    );
}

// 管理员可创建和修改账号；禁用或改密后旧登录会话失效，旧版本不能覆盖。
#[tokio::test]
async fn manage_account_disable_reset_and_revision_conflict() {
    let (_directory, _accounts, _clients, app, cookie, _sessions) = fixture().await;
    let account = create(&app, &cookie).await;
    assert_eq!(account["username"], "field.user");
    assert!(account.get("password_hash").is_none());
    assert!(account.get("password").is_none());
    let path = format!("/api/v1/accounts/{}", account["id"].as_str().unwrap());
    let signed = json(login(&app, "FIELD.USER", "initial-password").await).await;
    let token = signed["access_token"].as_str().unwrap();
    let edit = json!({"revision":1,"display_name":"已停用","enabled":false});
    assert_eq!(
        request(&app, "PUT", &path, &cookie, "", "account", "", edit.clone())
            .await
            .status(),
        StatusCode::OK
    );
    assert_eq!(
        request(&app, "PUT", &path, &cookie, "", "account", "", edit)
            .await
            .status(),
        StatusCode::CONFLICT
    );
    assert_eq!(
        request(
            &app,
            "GET",
            "/api/v1/client/auth/me",
            "",
            token,
            "",
            "",
            json!(null)
        )
        .await
        .status(),
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        login(&app, "field.user", "initial-password").await.status(),
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        request(
            &app,
            "PUT",
            &path,
            &cookie,
            "",
            "account",
            "",
            json!({"revision":2,"display_name":"重新启用","enabled":true})
        )
        .await
        .status(),
        StatusCode::OK
    );
    let signed = json(login(&app, "field.user", "initial-password").await).await;
    let old_token = signed["access_token"].as_str().unwrap();
    assert_eq!(
        request(
            &app,
            "POST",
            &format!("{path}/password"),
            &cookie,
            "",
            "account",
            "",
            json!({"revision":3,"password":"new-password"})
        )
        .await
        .status(),
        StatusCode::OK
    );
    assert_eq!(
        request(
            &app,
            "GET",
            "/api/v1/client/auth/me",
            "",
            old_token,
            "",
            "",
            json!(null)
        )
        .await
        .status(),
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        login(&app, "field.user", "initial-password").await.status(),
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        login(&app, "field.user", "new-password").await.status(),
        StatusCode::OK
    );
}

// 普通账号的会话只用于客户端登录，不能当成管理员Cookie或发布/更新权限。
#[tokio::test]
async fn client_sessions_cannot_escalate_to_administrator() {
    let (_directory, _accounts, _clients, app, cookie, _sessions) = fixture().await;
    create(&app, &cookie).await;
    let response = login(&app, "field.user", "initial-password").await;
    assert!(response.headers().get(header::SET_COOKIE).is_none());
    assert_eq!(response.headers()[header::CACHE_CONTROL], "no-store");
    let signed = json(response).await;
    let token = signed["access_token"].as_str().unwrap();
    assert_eq!(signed["token_type"], "Bearer");
    assert_eq!(
        request(
            &app,
            "GET",
            "/api/v1/client/auth/me",
            "",
            token,
            "",
            "",
            json!(null)
        )
        .await
        .status(),
        StatusCode::OK
    );
    assert_eq!(
        request(
            &app,
            "GET",
            "/api/v1/accounts",
            "",
            token,
            "",
            "",
            json!(null)
        )
        .await
        .status(),
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        request(
            &app,
            "GET",
            "/api/v1/auth/me",
            &format!("mskdsp_admin_session={token}"),
            "",
            "",
            "",
            json!(null)
        )
        .await
        .status(),
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        request(
            &app,
            "GET",
            "/api/v1/client/auth/me",
            &cookie,
            "",
            "",
            "",
            json!(null)
        )
        .await
        .status(),
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        request(
            &app,
            "POST",
            "/api/v1/client/auth/logout",
            "",
            token,
            "client-auth",
            "",
            json!({})
        )
        .await
        .status(),
        StatusCode::NO_CONTENT
    );
    assert_eq!(
        request(
            &app,
            "GET",
            "/api/v1/client/auth/me",
            "",
            token,
            "",
            "",
            json!(null)
        )
        .await
        .status(),
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        request(
            &app,
            "POST",
            "/api/v1/client/auth/logout",
            "",
            token,
            "client-auth",
            "",
            json!({})
        )
        .await
        .status(),
        StatusCode::NO_CONTENT
    );
}

// 重名、管理员保留名、弱密码、未知字段和超大请求均被拒绝。
#[tokio::test]
async fn invalid_input_reserved_name_and_duplicate_username() {
    let (_directory, _accounts, _clients, app, cookie, _sessions) = fixture().await;
    create(&app, &cookie).await;
    assert_eq!(
        request(
            &app,
            "POST",
            "/api/v1/accounts",
            &cookie,
            "",
            "account",
            "",
            json!({"username":"field.USER","password":"other-password"})
        )
        .await
        .status(),
        StatusCode::CONFLICT
    );
    for value in [
        json!({"username":"root","password":"valid-password"}),
        json!({"username":"field2","password":"short"}),
    ] {
        assert_eq!(
            request(
                &app,
                "POST",
                "/api/v1/accounts",
                &cookie,
                "",
                "account",
                "",
                value
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
            "/api/v1/accounts",
            &cookie,
            "",
            "account",
            "",
            json!({"username":"field2","password":"valid-password","role":"admin"})
        )
        .await
        .status(),
        StatusCode::UNPROCESSABLE_ENTITY
    );
    assert_eq!(
        request(
            &app,
            "POST",
            "/api/v1/accounts",
            &cookie,
            "",
            "account",
            "",
            json!({"username":"field2","password":"x".repeat(9000)})
        )
        .await
        .status(),
        StatusCode::PAYLOAD_TOO_LARGE
    );
    assert_eq!(
        login(&app, "unknown", "wrong-password").await.status(),
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        request(
            &app,
            "POST",
            "/api/v1/client/auth/login",
            "",
            "",
            "client-auth",
            "https://evil.example",
            json!({"username":"field.user","password":"initial-password"})
        )
        .await
        .status(),
        StatusCode::FORBIDDEN
    );
}

// 使用Web创建的账号登录并建立真实gRPC连接，管理员禁用后立即断开且在线数归零。
#[tokio::test]
async fn account_web_login_to_grpc_and_disable() {
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
    let (_directory, _accounts, clients, app, cookie, sessions) = fixture().await;
    let account = create(&app, &cookie).await;
    let signed = json(login(&app, "field.user", "initial-password").await).await;
    let token = signed["access_token"].as_str().unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let service = AgentGrpcService::new(sessions.clone(), "platform.v1".into(), clients);
    let server = tokio::spawn(async move {
        tonic::transport::Server::builder()
            .add_service(AgentServiceServer::new(service))
            .serve_with_incoming(TcpListenerStream::new(listener))
            .await
            .unwrap();
    });
    let mut client =
        agent::agent_service_client::AgentServiceClient::connect(format!("http://{address}"))
            .await
            .unwrap();
    let (sender, receiver) = mpsc::channel(2);
    sender
        .send(agent::AgentMessage {
            payload: Some(agent_message::Payload::Hello(agent::Hello {
                agent_id: String::new(),
                site_id: String::new(),
                agent_version: "test-v1".into(),
                protocol_version: "platform.v1".into(),
                capabilities: vec![],
            })),
        })
        .await
        .unwrap();
    let mut connect = tonic::Request::new(ReceiverStream::new(receiver));
    connect
        .metadata_mut()
        .insert("authorization", format!("Bearer {token}").parse().unwrap());
    let mut inbound = client.connect_agent(connect).await.unwrap().into_inner();
    assert!(matches!(
        inbound.message().await.unwrap().unwrap().payload,
        Some(server_message::Payload::Welcome(_))
    ));
    let path = format!("/api/v1/accounts/{}", account["id"].as_str().unwrap());
    let online = json(request(&app, "GET", &path, &cookie, "", "", "", json!(null)).await).await;
    assert_eq!(online["online_connections"], 1);
    let disabled = request(
        &app,
        "PUT",
        &path,
        &cookie,
        "",
        "account",
        "",
        json!({"revision":1,"display_name":"已禁用","enabled":false}),
    )
    .await;
    assert_eq!(disabled.status(), StatusCode::OK);
    assert_eq!(json(disabled).await["online_connections"], 0);
    let error = timeout(Duration::from_secs(2), inbound.message())
        .await
        .unwrap()
        .unwrap_err();
    assert_eq!(error.code(), tonic::Code::PermissionDenied);
    assert_eq!(sessions.count().await, 0);
    server.abort();
    let _ = server.await;
}
