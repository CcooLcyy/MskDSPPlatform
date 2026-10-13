use crate::{build_info::BuildInfo, state::SessionRegistry};
use axum::{
    Router,
    extract::State,
    http::StatusCode,
    response::{Html, IntoResponse},
    routing::get,
};
use serde::Serialize;
use std::sync::Arc;

#[derive(Clone)]
pub struct HttpState {
    pub sessions: SessionRegistry,
    pub protocol_version: Arc<str>,
}

pub fn router(state: HttpState) -> Router {
    Router::new()
        .route("/", get(update_page))
        .route("/update", get(update_page))
        .route("/healthz", get(healthz))
        .route("/readyz", get(readyz))
        .route("/api/v1/version", get(version))
        .with_state(state)
}

async fn update_page() -> Html<&'static str> {
    Html(include_str!("../web/update.html"))
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
