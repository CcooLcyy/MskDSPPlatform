use crate::state::SessionRegistry;
use axum::{Router, extract::State, http::StatusCode, response::IntoResponse, routing::get};
use serde::Serialize;
use std::sync::Arc;

#[derive(Clone)]
pub struct HttpState {
    pub sessions: SessionRegistry,
    pub protocol_version: Arc<str>,
}

pub fn router(state: HttpState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/readyz", get(readyz))
        .route("/api/v1/version", get(version))
        .with_state(state)
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
        service: "mskdsp-platform",
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
    service: &'static str,
    protocol_version: String,
}
