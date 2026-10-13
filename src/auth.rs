use crate::http::HttpState;
use anyhow::{Context, Result};
use argon2::{Argon2, PasswordHash, PasswordHasher, PasswordVerifier, password_hash::SaltString};
use axum::{
    Json,
    extract::{Request, State, rejection::JsonRejection},
    http::{HeaderMap, StatusCode, header},
    middleware::Next,
    response::{Html, IntoResponse, Redirect, Response},
};
use serde::{Deserialize, Serialize};
use std::{
    collections::HashMap,
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::sync::{Mutex, Semaphore};
use tracing::{info, warn};
use uuid::Uuid;

const COOKIE: &str = "mskdsp_admin_session";
const DEFAULT_HASH: &str = "$argon2id$v=19$m=19456,t=2,p=1$zXP+U5DsQuiMPtr8V8OuAQ$qhq/Ws7G6jkYExvvfYr/k4qqo1+/TOlpBo6DcKRl5ME";
const MAX_SESSIONS: usize = 128;

#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
pub struct AuthConfig {
    pub username: String,
    pub password_hash: String,
    pub secure_cookie: bool,
    pub session_seconds: u64,
}

impl Default for AuthConfig {
    fn default() -> Self {
        Self {
            username: "root".into(),
            password_hash: DEFAULT_HASH.into(),
            secure_cookie: false,
            session_seconds: 8 * 60 * 60,
        }
    }
}

#[derive(Clone)]
pub struct AdminAuth(Arc<AuthInner>);

struct AuthInner {
    config: AuthConfig,
    sessions: Mutex<HashMap<String, Instant>>,
    attempts: Mutex<(Instant, u32)>,
    verification_slots: Arc<Semaphore>,
}

impl AdminAuth {
    pub fn new(config: AuthConfig) -> Result<Self> {
        anyhow::ensure!(
            !config.username.is_empty()
                && config.username.len() <= 128
                && config.username.trim() == config.username,
            "管理员用户名必须为 1～128 字节且不能有首尾空白"
        );
        anyhow::ensure!(
            (1..=86400).contains(&config.session_seconds),
            "登录有效期必须为 1～86400 秒"
        );
        let hash = PasswordHash::new(&config.password_hash)
            .map_err(|_| anyhow::anyhow!("管理员密码哈希格式不正确"))?;
        anyhow::ensure!(
            hash.algorithm.as_str() == "argon2id" && hash.hash.is_some() && hash.salt.is_some(),
            "管理员密码必须使用完整的 Argon2id 哈希"
        );
        // 配置错误应在监听前暴露，不能在收到登录请求时才导致内部错误。
        match Argon2::default().verify_password(b"", &hash) {
            Ok(()) => anyhow::bail!("管理员密码不能为空"),
            Err(argon2::password_hash::Error::Password) => {}
            Err(_) => anyhow::bail!("管理员密码哈希参数不支持"),
        }
        Ok(Self(Arc::new(AuthInner {
            config,
            sessions: Mutex::new(HashMap::new()),
            attempts: Mutex::new((Instant::now(), 0)),
            verification_slots: Arc::new(Semaphore::new(2)),
        })))
    }

    async fn authenticated(&self, headers: &HeaderMap) -> bool {
        let Some(token) = session_token(headers) else {
            return false;
        };
        let mut sessions = self.0.sessions.lock().await;
        let now = Instant::now();
        sessions.retain(|_, expires| *expires > now);
        sessions.contains_key(token)
    }

    fn cookie(&self, token: &str, max_age: u64) -> String {
        format!(
            "{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{}",
            if self.0.config.secure_cookie {
                "; Secure"
            } else {
                ""
            }
        )
    }

    fn user(&self) -> UserResponse {
        UserResponse {
            username: self.0.config.username.clone(),
            role: "admin",
        }
    }
}

// 哈希工具只从标准输入读取密码，避免将明文密码放在命令参数中。
pub fn hash_password(password: &str) -> Result<String> {
    let salt = SaltString::encode_b64(Uuid::new_v4().as_bytes())
        .map_err(|_| anyhow::anyhow!("生成密码盐失败"))?;
    Argon2::default()
        .hash_password(password.as_bytes(), &salt)
        .map(|hash| hash.to_string())
        .map_err(|_| anyhow::anyhow!("生成密码哈希失败"))
}

fn session_token(headers: &HeaderMap) -> Option<&str> {
    let mut tokens = headers
        .get_all(header::COOKIE)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(';'))
        .filter_map(|part| part.trim().split_once('='))
        .filter(|(name, _)| *name == COOKIE)
        .map(|(_, token)| token);
    let token = tokens.next()?;
    // 重复会话 cookie 存在歧义，拒绝接受。
    (tokens.next().is_none() && token.len() == 64 && token.bytes().all(|c| c.is_ascii_hexdigit()))
        .then_some(token)
}

fn action_allowed(headers: &HeaderMap) -> bool {
    let actions: Vec<_> = headers.get_all("X-MskDSP-Action").iter().collect();
    if actions.len() != 1 || actions[0] != "auth" {
        return false;
    }
    let hosts: Vec<_> = headers.get_all(header::HOST).iter().collect();
    let origins: Vec<_> = headers.get_all(header::ORIGIN).iter().collect();
    if hosts.len() != 1 || origins.len() > 1 {
        return false;
    }
    let Ok(host) = hosts[0].to_str() else {
        return false;
    };
    if host.parse::<axum::http::uri::Authority>().is_err() || host.contains('@') {
        return false;
    }
    // 浏览器跨源 JSON 请求必须预检；本服务不开放 CORS。存在 Origin 时仍明确比对。
    origins.is_empty()
        || origins[0].to_str().is_ok_and(|origin| {
            origin.eq_ignore_ascii_case(&format!("http://{host}"))
                || origin.eq_ignore_ascii_case(&format!("https://{host}"))
        })
}

fn error(status: StatusCode, message: &str) -> Response {
    (status, Json(serde_json::json!({"error": message}))).into_response()
}

pub async fn no_cache(request: Request, next: Next) -> Response {
    let mut response = next.run(request).await;
    response
        .headers_mut()
        .insert(header::CACHE_CONTROL, "no-store".parse().unwrap());
    response
        .headers_mut()
        .insert(header::X_CONTENT_TYPE_OPTIONS, "nosniff".parse().unwrap());
    response.headers_mut().insert(header::CONTENT_SECURITY_POLICY,
        "default-src 'self'; script-src 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'".parse().unwrap());
    response
}

pub async fn require_admin(
    State(state): State<HttpState>,
    request: Request,
    next: Next,
) -> Response {
    if state.auth.authenticated(request.headers()).await {
        next.run(request).await
    } else {
        Redirect::to("/login").into_response()
    }
}

pub async fn login_page(State(state): State<HttpState>, headers: HeaderMap) -> Response {
    if state.auth.authenticated(&headers).await {
        Redirect::to("/update").into_response()
    } else {
        Html(include_str!("../web/login.html")).into_response()
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LoginRequest {
    username: String,
    password: String,
}

#[derive(Serialize)]
pub struct UserResponse {
    username: String,
    role: &'static str,
}

pub async fn login(
    State(state): State<HttpState>,
    headers: HeaderMap,
    body: std::result::Result<Json<LoginRequest>, JsonRejection>,
) -> Response {
    if !action_allowed(&headers) {
        return error(StatusCode::FORBIDDEN, "缺少操作标记或跨源请求");
    }
    let Json(body) = match body {
        Ok(body) => body,
        Err(rejection) => return error(rejection.status(), "登录请求格式不正确"),
    };
    if body.username.len() > 128 || body.password.len() > 1024 || body.password.is_empty() {
        return error(StatusCode::BAD_REQUEST, "用户名或密码长度不正确");
    }
    let auth = state.auth;
    {
        // 单管理员全局每分钟最多十次，限制猜测频率和密码验证消耗。
        let mut attempts = auth.0.attempts.lock().await;
        if attempts.0.elapsed() >= Duration::from_secs(60) {
            *attempts = (Instant::now(), 0);
        }
        if attempts.1 >= 10 {
            let mut response = error(
                StatusCode::TOO_MANY_REQUESTS,
                "登录尝试过于频繁，请一分钟后重试",
            );
            response
                .headers_mut()
                .insert(header::RETRY_AFTER, "60".parse().unwrap());
            return response;
        }
        attempts.1 += 1;
    }
    let Ok(permit) = auth.0.verification_slots.clone().try_acquire_owned() else {
        return error(StatusCode::TOO_MANY_REQUESTS, "登录请求繁忙，请稍后重试");
    };
    let hash = auth.0.config.password_hash.clone();
    let valid_username = body.username == auth.0.config.username;
    let result = tokio::task::spawn_blocking(move || {
        let _permit = permit;
        // 无论用户名是否正确都验证哈希，不通过耗时泄露用户名是否存在。
        PasswordHash::new(&hash).is_ok_and(|hash| {
            Argon2::default()
                .verify_password(body.password.as_bytes(), &hash)
                .is_ok()
        }) && valid_username
    })
    .await
    .context("管理员密码验证任务失败");
    match result {
        Ok(true) => {}
        Ok(false) => {
            warn!("管理员登录失败：用户名或密码不正确");
            return error(StatusCode::UNAUTHORIZED, "用户名或密码不正确");
        }
        Err(_) => {
            return error(
                StatusCode::SERVICE_UNAVAILABLE,
                "暂时无法验证登录，请稍后重试",
            );
        }
    }
    let token = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
    let mut sessions = auth.0.sessions.lock().await;
    let now = Instant::now();
    sessions.retain(|_, expires| *expires > now);
    if let Some(old) = session_token(&headers) {
        sessions.remove(old);
    }
    if sessions.len() >= MAX_SESSIONS {
        return error(
            StatusCode::TOO_MANY_REQUESTS,
            "当前登录会话过多，请稍后重试",
        );
    }
    sessions.insert(
        token.clone(),
        now + Duration::from_secs(auth.0.config.session_seconds),
    );
    let mut response = Json(auth.user()).into_response();
    response.headers_mut().insert(
        header::SET_COOKIE,
        auth.cookie(&token, auth.0.config.session_seconds)
            .parse()
            .unwrap(),
    );
    info!("管理员登录成功");
    response
}

pub async fn me(State(state): State<HttpState>, headers: HeaderMap) -> Response {
    if state.auth.authenticated(&headers).await {
        Json(state.auth.user()).into_response()
    } else {
        error(StatusCode::UNAUTHORIZED, "请先登录管理员账号")
    }
}

pub async fn logout(State(state): State<HttpState>, headers: HeaderMap) -> Response {
    if !action_allowed(&headers) {
        return error(StatusCode::FORBIDDEN, "缺少操作标记或跨源请求");
    }
    if let Some(token) = session_token(&headers) {
        state.auth.0.sessions.lock().await.remove(token);
    }
    let mut response = StatusCode::NO_CONTENT.into_response();
    response.headers_mut().insert(
        header::SET_COOKIE,
        state.auth.cookie("", 0).parse().unwrap(),
    );
    info!("管理员会话已退出");
    response
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{http::router, state::SessionRegistry};
    use axum::{
        body::{Body, to_bytes},
        http::Request,
    };
    use tower::ServiceExt;

    fn fixture(secure: bool) -> (AdminAuth, axum::Router) {
        let salt = SaltString::encode_b64(b"test random salt").unwrap();
        let kdf = Argon2::new(
            argon2::Algorithm::Argon2id,
            argon2::Version::V0x13,
            argon2::Params::new(32, 1, 1, None).unwrap(),
        );
        let auth = AdminAuth::new(AuthConfig {
            password_hash: kdf
                .hash_password(b"test-password", &salt)
                .unwrap()
                .to_string(),
            secure_cookie: secure,
            ..AuthConfig::default()
        })
        .unwrap();
        let app = router(HttpState {
            sessions: SessionRegistry::default(),
            protocol_version: "platform.v1".into(),
            auth: auth.clone(),
        });
        (auth, app)
    }

    async fn request(
        app: &axum::Router,
        method: &str,
        path: &str,
        cookie: &str,
        action: &str,
        origin: &str,
        body: &str,
    ) -> Response {
        let mut req = Request::builder()
            .method(method)
            .uri(path)
            .header(header::HOST, "platform.example")
            .header(header::CONTENT_TYPE, "application/json");
        if !cookie.is_empty() {
            req = req.header(header::COOKIE, cookie);
        }
        if !action.is_empty() {
            req = req.header("X-MskDSP-Action", action);
        }
        if !origin.is_empty() {
            req = req.header(header::ORIGIN, origin);
        }
        app.clone()
            .oneshot(req.body(Body::from(body.to_owned())).unwrap())
            .await
            .unwrap()
    }

    async fn sign_in(app: &axum::Router, cookie: &str) -> Response {
        request(
            app,
            "POST",
            "/api/v1/auth/login",
            cookie,
            "auth",
            "https://platform.example",
            r#"{"username":"root","password":"test-password"}"#,
        )
        .await
    }

    fn cookie(response: &Response) -> String {
        response.headers()[header::SET_COOKIE]
            .to_str()
            .unwrap()
            .split(';')
            .next()
            .unwrap()
            .into()
    }

    // 匿名页面跳登录，公开探针仍可用，伪造和重复 cookie 不提供身份。
    #[tokio::test]
    async fn anonymous_pages_and_invalid_sessions() {
        let (_, app) = fixture(false);
        for path in ["/", "/update"] {
            let response = request(&app, "GET", path, "", "", "", "").await;
            assert_eq!(response.status(), StatusCode::SEE_OTHER);
            assert_eq!(response.headers()[header::LOCATION], "/login");
            assert_eq!(response.headers()[header::CACHE_CONTROL], "no-store");
        }
        for path in ["/login", "/healthz", "/readyz", "/api/v1/version"] {
            assert_eq!(
                request(&app, "GET", path, "", "", "", "").await.status(),
                StatusCode::OK
            );
        }
        let fake = format!("{COOKIE}={}", "a".repeat(64));
        assert_eq!(
            request(&app, "GET", "/api/v1/auth/me", &fake, "", "", "")
                .await
                .status(),
            StatusCode::UNAUTHORIZED
        );
        let session = cookie(&sign_in(&app, "").await);
        let duplicate = format!("{session}; {session}");
        assert_eq!(
            request(&app, "GET", "/api/v1/auth/me", &duplicate, "", "", "")
                .await
                .status(),
            StatusCode::UNAUTHORIZED
        );
    }

    // 正确登录设置安全 cookie，重复登录轮换 token，退出撤销身份。
    #[tokio::test]
    async fn login_rotation_and_logout() {
        let (_, app) = fixture(true);
        let response = sign_in(&app, "").await;
        assert_eq!(response.status(), StatusCode::OK);
        let attributes = response.headers()[header::SET_COOKIE].to_str().unwrap();
        for attr in [
            "HttpOnly",
            "SameSite=Strict",
            "Path=/",
            "Max-Age=28800",
            "Secure",
        ] {
            assert!(attributes.contains(attr));
        }
        let first = cookie(&response);
        let response = sign_in(&app, &first).await;
        let second = cookie(&response);
        assert_ne!(first, second);
        assert_eq!(
            request(&app, "GET", "/api/v1/auth/me", &first, "", "", "")
                .await
                .status(),
            StatusCode::UNAUTHORIZED
        );
        let response = request(&app, "GET", "/api/v1/auth/me", &second, "", "", "").await;
        assert_eq!(response.status(), StatusCode::OK);
        let user: serde_json::Value =
            serde_json::from_slice(&to_bytes(response.into_body(), 4096).await.unwrap()).unwrap();
        assert_eq!(user, serde_json::json!({"username":"root", "role":"admin"}));
        assert_eq!(
            request(&app, "GET", "/update", &second, "", "", "")
                .await
                .status(),
            StatusCode::OK
        );
        let response = request(&app, "GET", "/login", &second, "", "", "").await;
        assert_eq!(response.headers()[header::LOCATION], "/update");
        let response = request(
            &app,
            "POST",
            "/api/v1/auth/logout",
            &second,
            "auth",
            "",
            "{}",
        )
        .await;
        assert_eq!(response.status(), StatusCode::NO_CONTENT);
        assert!(
            response.headers()[header::SET_COOKIE]
                .to_str()
                .unwrap()
                .contains("Max-Age=0")
        );
        assert_eq!(
            request(&app, "GET", "/api/v1/auth/me", &second, "", "", "")
                .await
                .status(),
            StatusCode::UNAUTHORIZED
        );
    }

    // 登录失败不给 cookie，限流达到上限后返回 429 并在窗口结束后恢复。
    #[tokio::test]
    async fn invalid_credentials_and_rate_limit() {
        let (auth, app) = fixture(false);
        for i in 0..10 {
            let body = if i % 2 == 0 {
                r#"{"username":"root","password":"wrong"}"#
            } else {
                r#"{"username":"other","password":"test-password"}"#
            };
            let response = request(&app, "POST", "/api/v1/auth/login", "", "auth", "", body).await;
            assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
            assert!(!response.headers().contains_key(header::SET_COOKIE));
        }
        let response = sign_in(&app, "").await;
        assert_eq!(response.status(), StatusCode::TOO_MANY_REQUESTS);
        assert_eq!(response.headers()[header::RETRY_AFTER], "60");
        auth.0.attempts.lock().await.0 = Instant::now() - Duration::from_secs(61);
        assert_eq!(sign_in(&app, "").await.status(), StatusCode::OK);
    }

    // 会话过期和服务实例重建都使原 cookie 失效，不依赖浏览器自行删除。
    #[tokio::test]
    async fn expiry_and_restart_revoke_sessions() {
        let (auth, app) = fixture(false);
        let session = cookie(&sign_in(&app, "").await);
        let (_, restarted) = fixture(false);
        assert_eq!(
            request(&restarted, "GET", "/api/v1/auth/me", &session, "", "", "")
                .await
                .status(),
            StatusCode::UNAUTHORIZED
        );
        for expiry in auth.0.sessions.lock().await.values_mut() {
            *expiry = Instant::now();
        }
        assert_eq!(
            request(&app, "GET", "/api/v1/auth/me", &session, "", "", "")
                .await
                .status(),
            StatusCode::UNAUTHORIZED
        );
        assert!(auth.0.sessions.lock().await.is_empty());
    }

    // 缺少操作标记、跨源、畸形及超大请求被拒绝；非法退出不能撤销会话。
    #[tokio::test]
    async fn csrf_and_request_boundaries() {
        let (_, app) = fixture(false);
        for (action, origin) in [("", ""), ("auth", "https://evil.example"), ("auth", "null")] {
            assert_eq!(
                request(&app, "POST", "/api/v1/auth/login", "", action, origin, "{}")
                    .await
                    .status(),
                StatusCode::FORBIDDEN
            );
        }
        assert_eq!(
            request(&app, "POST", "/api/v1/auth/login", "", "auth", "", "{}")
                .await
                .status(),
            StatusCode::UNPROCESSABLE_ENTITY
        );
        assert_eq!(
            request(
                &app,
                "POST",
                "/api/v1/auth/login",
                "",
                "auth",
                "",
                &"x".repeat(4097)
            )
            .await
            .status(),
            StatusCode::PAYLOAD_TOO_LARGE
        );
        let session = cookie(&sign_in(&app, "").await);
        assert_eq!(
            request(
                &app,
                "POST",
                "/api/v1/auth/logout",
                &session,
                "auth",
                "https://evil.example",
                "{}"
            )
            .await
            .status(),
            StatusCode::FORBIDDEN
        );
        assert_eq!(
            request(&app, "GET", "/api/v1/auth/me", &session, "", "", "")
                .await
                .status(),
            StatusCode::OK
        );
    }

    // 无效哈希、空密码哈希和越界会话有效期拒绝构建认证服务。
    #[test]
    fn invalid_configuration_rejected() {
        assert!(
            AdminAuth::new(AuthConfig {
                password_hash: "invalid".into(),
                ..AuthConfig::default()
            })
            .is_err()
        );
        assert!(
            AdminAuth::new(AuthConfig {
                password_hash: hash_password("").unwrap(),
                ..AuthConfig::default()
            })
            .is_err()
        );
        assert!(
            AdminAuth::new(AuthConfig {
                session_seconds: 0,
                ..AuthConfig::default()
            })
            .is_err()
        );
    }
}
