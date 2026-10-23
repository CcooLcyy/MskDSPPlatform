use super::*;
use crate::{
    auth::{AdminAuth, AuthConfig, hash_password},
    state::SessionRegistry,
};
use axum::{
    body::{Body, to_bytes},
    http::{Request, header},
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

#[tokio::test]
async fn delete_requires_web_admin_origin_action_and_current_revision() {
    let (_directory, accounts, clients, app, cookie, sessions) = fixture().await;
    let account = create(&app, &cookie).await;
    let id = account["id"].as_str().unwrap();
    let path = format!("/api/v1/accounts/{id}");
    let signed = clients
        .login("field.user", "initial-password")
        .await
        .unwrap();
    let cancellation = sessions
        .register(crate::state::AgentSession {
            connection_id: "测试连接".into(),
            agent_id: "测试客户端".into(),
            account_id: id.into(),
        })
        .await
        .unwrap();
    for (admin, bearer, action, origin, expected) in [
        ("", "", "account", "", StatusCode::UNAUTHORIZED),
        (
            "",
            signed.access_token.as_str(),
            "account",
            "",
            StatusCode::UNAUTHORIZED,
        ),
        (cookie.as_str(), "", "", "", StatusCode::FORBIDDEN),
        (
            cookie.as_str(),
            "",
            "account",
            "https://other.example",
            StatusCode::FORBIDDEN,
        ),
    ] {
        assert_eq!(
            request(
                &app,
                "DELETE",
                &path,
                admin,
                bearer,
                action,
                origin,
                json!({"revision":1})
            )
            .await
            .status(),
            expected
        );
        accounts.get(id).await.unwrap();
    }
    assert_eq!(
        request(
            &app,
            "DELETE",
            &path,
            &cookie,
            "",
            "account",
            "",
            json!({"revision":99})
        )
        .await
        .status(),
        StatusCode::CONFLICT
    );
    clients.authenticate(&signed.access_token).await.unwrap();
    assert!(!cancellation.is_cancelled(), "冲突不得断开有效连接");
    for value in [json!({}), json!({"revision":1,"extra":true})] {
        assert_eq!(
            request(&app, "DELETE", &path, &cookie, "", "account", "", value)
                .await
                .status(),
            StatusCode::UNPROCESSABLE_ENTITY
        );
    }
    assert_eq!(
        request(
            &app,
            "DELETE",
            &path,
            &cookie,
            "",
            "account",
            "http://platform.example",
            json!({"revision":1})
        )
        .await
        .status(),
        StatusCode::NO_CONTENT
    );
    assert!(matches!(
        accounts.get(id).await,
        Err(AccountError::NotFound)
    ));
    assert!(cancellation.is_cancelled(), "删除成功后必须断开已有连接");
    assert_eq!(sessions.count_for_account(id).await, 0);
    assert!(matches!(
        clients.authenticate(&signed.access_token).await,
        Err(crate::client_auth::ClientAuthError::Unauthorized)
    ));
    assert_eq!(
        request(&app, "GET", &path, &cookie, "", "", "", json!(null))
            .await
            .status(),
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        request(
            &app,
            "DELETE",
            &path,
            &cookie,
            "",
            "account",
            "",
            json!({"revision":1})
        )
        .await
        .status(),
        StatusCode::NOT_FOUND
    );
}

#[tokio::test]
async fn web_admin_assigns_roles_without_revoking_sessions_and_client_admin_cannot_manage() {
    let (_directory, accounts, clients, app, cookie, _sessions) = fixture().await;
    let account = create(&app, &cookie).await;
    assert_eq!(account["role"], "operator");
    let id = account["id"].as_str().unwrap();
    let path = format!("/api/v1/accounts/{id}");
    let signed = clients
        .login("field.user", "initial-password")
        .await
        .unwrap();
    let update = json!({"revision":1,"display_name":"现场管理员","enabled":true,"role":"admin"});
    for (action, origin) in [("", ""), ("account", "https://evil.example")] {
        assert_eq!(
            request(
                &app,
                "PUT",
                &path,
                &cookie,
                "",
                action,
                origin,
                update.clone()
            )
            .await
            .status(),
            StatusCode::FORBIDDEN
        );
    }
    let response = request(
        &app,
        "PUT",
        &path,
        &cookie,
        "",
        "account",
        "",
        update.clone(),
    )
    .await;
    assert_eq!(response.status(), StatusCode::OK);
    let admin = json(response).await;
    assert_eq!(admin["role"], "admin");
    assert_eq!(admin["revision"], 2);
    assert_eq!(
        clients
            .authenticate(&signed.access_token)
            .await
            .unwrap()
            .account
            .role,
        "admin"
    );
    assert_eq!(
        request(&app, "PUT", &path, &cookie, "", "account", "", update)
            .await
            .status(),
        StatusCode::CONFLICT
    );
    let detail = json(request(&app, "GET", &path, &cookie, "", "", "", json!(null)).await).await;
    assert_eq!(detail["role"], "admin");
    let list = json(
        request(
            &app,
            "GET",
            "/api/v1/accounts",
            &cookie,
            "",
            "",
            "",
            json!(null),
        )
        .await,
    )
    .await;
    assert_eq!(list[0]["role"], "admin");
    // 上位机管理员仍然不是平台 Web 管理员，不能使用自身会话改角色。
    for cookie in [
        String::new(),
        format!("mskdsp_admin_session={}", signed.access_token),
    ] {
        assert_eq!(
            request(
                &app,
                "PUT",
                &path,
                &cookie,
                &signed.access_token,
                "account",
                "",
                json!({"revision":2,"display_name":"越权","enabled":true,"role":"operator"})
            )
            .await
            .status(),
            StatusCode::UNAUTHORIZED
        );
    }
    for role in ["", "superadmin", "ADMIN"] {
        assert_eq!(
            request(
                &app,
                "PUT",
                &path,
                &cookie,
                "",
                "account",
                "",
                json!({"revision":2,"display_name":"非法更新","enabled":false,"role":role})
            )
            .await
            .status(),
            StatusCode::BAD_REQUEST
        );
        let current = accounts.get(id).await.unwrap();
        assert_eq!(current.revision, 2);
        assert!(current.enabled);
        assert_eq!(current.role, "admin");
        assert_eq!(
            request(
                &app,
                "POST",
                "/api/v1/accounts",
                &cookie,
                "",
                "account",
                "",
                json!({"username":"invalid-role","password":"valid-password","role":role})
            )
            .await
            .status(),
            StatusCode::BAD_REQUEST
        );
    }
    let response = request(
        &app,
        "PUT",
        &path,
        &cookie,
        "",
        "account",
        "",
        json!({"revision":2,"display_name":"兼容旧管理端","enabled":true}),
    )
    .await;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(json(response).await["role"], "admin");
    let response = request(
        &app,
        "POST",
        "/api/v1/accounts",
        &cookie,
        "",
        "account",
        "",
        json!({"username":"second-admin","password":"valid-password","role":"admin"}),
    )
    .await;
    assert_eq!(response.status(), StatusCode::CREATED);
    assert_eq!(json(response).await["role"], "admin");
}

// 客户端只能使用 gRPC；旧 HTTP 路径对匿名、管理员和普通会话均返回 404，也不撤销有效会话。
#[tokio::test]
async fn client_http_auth_routes_are_absent_and_do_not_create_or_revoke_sessions() {
    let (_directory, accounts, clients, app, cookie, sessions) = fixture().await;
    let account = create(&app, &cookie).await;
    let before = accounts.get(account["id"].as_str().unwrap()).await.unwrap();
    assert!(before.last_login_at.is_none());
    for (method, path) in [
        ("POST", "/api/v1/client/auth/login"),
        ("GET", "/api/v1/client/auth/me"),
        ("POST", "/api/v1/client/auth/logout"),
    ] {
        for admin in ["", cookie.as_str()] {
            assert_eq!(
                request(
                    &app,
                    method,
                    path,
                    admin,
                    "",
                    "client-auth",
                    "",
                    json!({"username":"field.user","password":"initial-password"})
                )
                .await
                .status(),
                StatusCode::NOT_FOUND
            );
        }
    }
    assert!(
        accounts
            .get(&before.id)
            .await
            .unwrap()
            .last_login_at
            .is_none(),
        "废弃入口不得执行登录或生成会话"
    );
    assert_eq!(sessions.count().await, 0);
    let signed = clients
        .login("field.user", "initial-password")
        .await
        .unwrap();
    for (method, path) in [
        ("POST", "/api/v1/client/auth/login"),
        ("GET", "/api/v1/client/auth/me"),
        ("POST", "/api/v1/client/auth/logout"),
    ] {
        assert_eq!(
            request(
                &app,
                method,
                path,
                "",
                &signed.access_token,
                "client-auth",
                "",
                json!({"username":"field.user","password":"initial-password"})
            )
            .await
            .status(),
            StatusCode::NOT_FOUND
        );
    }
    clients.authenticate(&signed.access_token).await.unwrap();
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
    let (_directory, _accounts, clients, app, cookie, _sessions) = fixture().await;
    let account = create(&app, &cookie).await;
    assert_eq!(account["username"], "field.user");
    assert!(account.get("password_hash").is_none());
    assert!(account.get("password").is_none());
    let path = format!("/api/v1/accounts/{}", account["id"].as_str().unwrap());
    let signed = clients
        .login("FIELD.USER", "initial-password")
        .await
        .unwrap();
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
    assert!(matches!(
        clients.authenticate(&signed.access_token).await,
        Err(crate::client_auth::ClientAuthError::Unauthorized)
    ));
    assert!(matches!(
        clients.login("field.user", "initial-password").await,
        Err(crate::client_auth::ClientAuthError::Unauthorized)
    ));
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
    assert!(
        matches!(
            clients.authenticate(&signed.access_token).await,
            Err(crate::client_auth::ClientAuthError::Unauthorized)
        ),
        "重新启用不恢复旧会话"
    );
    let fresh = clients
        .login("field.user", "initial-password")
        .await
        .unwrap();
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
    assert!(matches!(
        clients.authenticate(&fresh.access_token).await,
        Err(crate::client_auth::ClientAuthError::Unauthorized)
    ));
    assert!(matches!(
        clients.login("field.user", "initial-password").await,
        Err(crate::client_auth::ClientAuthError::Unauthorized)
    ));
    clients.login("field.user", "new-password").await.unwrap();
}

// 普通账号会话不能当成管理员 Cookie，也不能以 Bearer 越权读取账号管理数据。
#[tokio::test]
async fn client_sessions_cannot_escalate_to_administrator() {
    let (_directory, _accounts, clients, app, cookie, _sessions) = fixture().await;
    create(&app, &cookie).await;
    let signed = clients
        .login("field.user", "initial-password")
        .await
        .unwrap();
    let token = &signed.access_token;
    assert_eq!(signed.token_type, "Bearer");
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
            "POST",
            "/api/v1/accounts",
            "",
            token,
            "account",
            "",
            json!({"username":"forbidden","password":"valid-password"})
        )
        .await
        .status(),
        StatusCode::UNAUTHORIZED
    );
    clients.authenticate(token).await.unwrap();
    clients.logout(token).await.unwrap();
    assert!(matches!(
        clients.authenticate(token).await,
        Err(crate::client_auth::ClientAuthError::Unauthorized)
    ));
    clients.logout(token).await.unwrap();
}

// 重名、管理员保留名、弱密码、未知字段和超大请求均被拒绝。
#[tokio::test]
async fn invalid_input_reserved_name_and_duplicate_username() {
    let (_directory, _accounts, clients, app, cookie, _sessions) = fixture().await;
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
            json!({"username":"field2","password":"valid-password","unknown_role":"admin"})
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
    assert!(matches!(
        clients.login("unknown", "wrong-password").await,
        Err(crate::client_auth::ClientAuthError::Unauthorized)
    ));
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
    let signed = client
        .login(agent::LoginRequest {
            username: "field.user".into(),
            password: "initial-password".into(),
        })
        .await
        .unwrap()
        .into_inner();
    let token = &signed.access_token;
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
