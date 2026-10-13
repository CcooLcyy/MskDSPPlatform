use crate::{
    auth::{self, AdminAuth},
    build_info::BuildInfo,
    state::SessionRegistry,
};
use axum::{
    Router,
    extract::DefaultBodyLimit,
    extract::State,
    http::{StatusCode, header},
    middleware,
    response::{Html, IntoResponse},
    routing::{get, post},
};
use serde::Serialize;
use std::sync::Arc;

#[derive(Clone)]
pub struct HttpState {
    pub sessions: SessionRegistry,
    pub protocol_version: Arc<str>,
    pub auth: AdminAuth,
}

pub fn router(state: HttpState) -> Router {
    Router::new()
        .route("/", get(crate::template_http::page))
        .route("/update", get(update_page))
        .route("/modbus-templates", get(crate::template_http::page))
        .route_layer(middleware::from_fn_with_state(
            state.clone(),
            auth::require_admin,
        ))
        .route("/login", get(auth::login_page))
        .route("/assets/platform.css", get(platform_styles))
        .route("/api/v1/auth/login", post(auth::login))
        .route("/api/v1/auth/logout", post(auth::logout))
        .route("/api/v1/auth/me", get(auth::me))
        .route("/healthz", get(healthz))
        .route("/readyz", get(readyz))
        .route("/api/v1/version", get(version))
        .layer(DefaultBodyLimit::max(4096))
        .layer(middleware::from_fn(auth::no_cache))
        .with_state(state)
}

async fn update_page() -> Html<&'static str> {
    Html(include_str!("../web/update.html"))
}

async fn platform_styles() -> impl IntoResponse {
    (
        [(header::CONTENT_TYPE, "text/css; charset=utf-8")],
        include_str!("../web/platform.css"),
    )
}

async fn healthz() -> impl IntoResponse {
    StatusCode::OK
}

async fn readyz(State(state): State<HttpState>) -> impl IntoResponse {
    let body = HealthResponse {
        status: "ready",
        online_agents: state.sessions.count().await,
    };
    (StatusCode::OK, axum::Json(body))
}

async fn version(State(state): State<HttpState>) -> impl IntoResponse {
    axum::Json(VersionResponse {
        build: BuildInfo::current(),
        protocol_version: state.protocol_version.to_string(),
    })
}

#[derive(Serialize)]
struct HealthResponse {
    status: &'static str,
    online_agents: usize,
}

#[derive(Serialize)]
struct VersionResponse {
    protocol_version: String,
    // HTTP 与命令行共享编译身份，运行配置不能改变发布版本。
    #[serde(flatten)]
    build: BuildInfo,
}
