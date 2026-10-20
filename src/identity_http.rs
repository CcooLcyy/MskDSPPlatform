use crate::{
    auth,
    http::HttpState,
    identities::{AgentRecord, AgentUpdate, IdentityError, IdentityStore, NewAgent},
};
use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, Path, Request, State, rejection::JsonRejection},
    http::{HeaderMap, StatusCode, header},
    middleware::{self, Next},
    response::{Html, IntoResponse, Response},
    routing::{get, post},
};
use serde::{Deserialize, Serialize};
use serde_json::json;

#[derive(Clone)]
struct IdentityState {
    http: HttpState,
    store: IdentityStore,
}

pub fn router(http: HttpState, store: IdentityStore) -> Router {
    let state = IdentityState { http, store };
    Router::new()
        .route(
            "/agents",
            get(page).route_layer(middleware::from_fn_with_state(
                state.http.clone(),
                auth::require_admin,
            )),
        )
        .merge(
            Router::new()
                .route("/api/v1/agents", get(list).post(create))
                .route("/api/v1/agents/{id}", get(detail).put(update))
                .route("/api/v1/agents/{id}/rotate", post(rotate))
                .route("/api/v1/agents/{id}/revoke", post(revoke))
                .route("/api/v1/agents/{id}/unbind", post(unbind))
                .route("/api/v1/agents/{id}/events", get(events))
                .route_layer(middleware::from_fn_with_state(state.clone(), require_admin)),
        )
        .layer(DefaultBodyLimit::max(16 * 1024))
        .layer(middleware::from_fn(auth::no_cache))
        .with_state(state)
}

async fn page() -> Html<&'static str> {
    Html(include_str!("../web/agents.html"))
}

async fn require_admin(
    State(state): State<IdentityState>,
    request: Request,
    next: Next,
) -> Response {
    let identity = auth::me(State(state.http), request.headers().clone()).await;
    if identity.status() == StatusCode::OK {
        next.run(request).await
    } else {
        identity
    }
}

fn error(status: StatusCode, message: impl ToString) -> Response {
    (status, Json(json!({"error":message.to_string()}))).into_response()
}

impl IntoResponse for IdentityError {
    fn into_response(self) -> Response {
        let status = match self {
            Self::Invalid(_) => StatusCode::BAD_REQUEST,
            Self::NotFound => StatusCode::NOT_FOUND,
            Self::Conflict => StatusCode::CONFLICT,
            Self::Unauthorized => StatusCode::UNAUTHORIZED,
            Self::Storage => StatusCode::INTERNAL_SERVER_ERROR,
        };
        error(status, self)
    }
}

struct RequestError(StatusCode, &'static str);

impl IntoResponse for RequestError {
    fn into_response(self) -> Response {
        error(self.0, self.1)
    }
}

fn action(headers: &HeaderMap) -> Result<(), RequestError> {
    let actions: Vec<_> = headers.get_all("X-MskDSP-Action").iter().collect();
    let hosts: Vec<_> = headers.get_all(header::HOST).iter().collect();
    let origins: Vec<_> = headers.get_all(header::ORIGIN).iter().collect();
    if actions.len() != 1 || actions[0] != "agent-identity" || hosts.len() != 1 || origins.len() > 1
    {
        return Err(RequestError(
            StatusCode::FORBIDDEN,
            "缺少身份管理操作标记或请求来源不正确",
        ));
    }
    let host = hosts[0].to_str().unwrap_or_default();
    if host.contains('@')
        || host.parse::<axum::http::uri::Authority>().is_err()
        || origins.first().is_some_and(|origin| {
            !origin.to_str().is_ok_and(|value| {
                value.eq_ignore_ascii_case(&format!("http://{host}"))
                    || value.eq_ignore_ascii_case(&format!("https://{host}"))
            })
        })
    {
        return Err(RequestError(
            StatusCode::FORBIDDEN,
            "不允许跨源身份管理请求",
        ));
    }
    Ok(())
}

fn body<T>(value: Result<Json<T>, JsonRejection>) -> Result<T, RequestError> {
    value.map(|Json(body)| body).map_err(|rejection| {
        RequestError(rejection.status(), "身份管理请求格式不正确或超过 16 KiB")
    })
}

#[derive(Serialize)]
struct View {
    #[serde(flatten)]
    agent: AgentRecord,
    online: bool,
    connection_id: Option<String>,
}

async fn view(state: &IdentityState, mut agent: AgentRecord) -> View {
    let session = state.http.sessions.get(&agent.id).await;
    let online = agent.enabled
        && agent.credential_active
        && session
            .as_ref()
            .is_some_and(|session| session.site_id == agent.site_id);
    if let Some(session) = session.as_ref().filter(|_| online) {
        agent.agent_version = session.agent_version.clone();
    }
    View {
        agent,
        online,
        connection_id: session
            .filter(|_| online)
            .map(|session| session.connection_id),
    }
}

async fn list(State(state): State<IdentityState>) -> Response {
    match state.store.list().await {
        Ok(records) => {
            let mut views = Vec::with_capacity(records.len());
            for record in records {
                views.push(view(&state, record).await);
            }
            Json(views).into_response()
        }
        Err(error) => error.into_response(),
    }
}

async fn detail(State(state): State<IdentityState>, Path(id): Path<String>) -> Response {
    match state.store.get(&id).await {
        Ok(record) => Json(view(&state, record).await).into_response(),
        Err(error) => error.into_response(),
    }
}

async fn create(
    State(state): State<IdentityState>,
    headers: HeaderMap,
    value: Result<Json<NewAgent>, JsonRejection>,
) -> Response {
    if let Err(response) = action(&headers) {
        return response.into_response();
    }
    let input = match body(value) {
        Ok(input) => input,
        Err(response) => return response.into_response(),
    };
    match state
        .store
        .register(input, state.http.auth.username())
        .await
    {
        Ok(issued) => (
            StatusCode::CREATED,
            Json(json!({"agent":view(&state,issued.agent).await,"token":issued.token})),
        )
            .into_response(),
        Err(error) => error.into_response(),
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct UpdateRequest {
    revision: i64,
    name: String,
    site_id: String,
    notes: String,
    enabled: bool,
}

async fn update(
    State(state): State<IdentityState>,
    Path(id): Path<String>,
    headers: HeaderMap,
    value: Result<Json<UpdateRequest>, JsonRejection>,
) -> Response {
    if let Err(response) = action(&headers) {
        return response.into_response();
    }
    let input = match body(value) {
        Ok(input) => input,
        Err(response) => return response.into_response(),
    };
    let previous = match state.store.get(&id).await {
        Ok(record) => record,
        Err(error) => return error.into_response(),
    };
    let changed = previous.enabled != input.enabled || previous.site_id != input.site_id.trim();
    match state
        .store
        .update(
            &id,
            input.revision,
            AgentUpdate {
                name: input.name,
                site_id: input.site_id,
                notes: input.notes,
                enabled: input.enabled,
            },
            state.http.auth.username(),
        )
        .await
    {
        Ok(record) => {
            if changed {
                state.http.sessions.disconnect(&id).await;
            }
            Json(view(&state, record).await).into_response()
        }
        Err(error) => error.into_response(),
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Revision {
    revision: i64,
}

async fn rotate(
    State(state): State<IdentityState>,
    Path(id): Path<String>,
    headers: HeaderMap,
    value: Result<Json<Revision>, JsonRejection>,
) -> Response {
    if let Err(response) = action(&headers) {
        return response.into_response();
    }
    let input = match body(value) {
        Ok(input) => input,
        Err(response) => return response.into_response(),
    };
    match state
        .store
        .rotate(&id, input.revision, state.http.auth.username())
        .await
    {
        Ok(issued) => {
            state.http.sessions.disconnect(&id).await;
            Json(json!({"agent":view(&state,issued.agent).await,"token":issued.token}))
                .into_response()
        }
        Err(error) => error.into_response(),
    }
}

async fn revoke(
    State(state): State<IdentityState>,
    Path(id): Path<String>,
    headers: HeaderMap,
    value: Result<Json<Revision>, JsonRejection>,
) -> Response {
    withdraw(state, id, headers, value, false).await
}

async fn unbind(
    State(state): State<IdentityState>,
    Path(id): Path<String>,
    headers: HeaderMap,
    value: Result<Json<Revision>, JsonRejection>,
) -> Response {
    withdraw(state, id, headers, value, true).await
}

async fn withdraw(
    state: IdentityState,
    id: String,
    headers: HeaderMap,
    value: Result<Json<Revision>, JsonRejection>,
    unbind: bool,
) -> Response {
    if let Err(response) = action(&headers) {
        return response.into_response();
    }
    let input = match body(value) {
        Ok(input) => input,
        Err(response) => return response.into_response(),
    };
    let result = if unbind {
        state
            .store
            .unbind(&id, input.revision, state.http.auth.username())
            .await
    } else {
        state
            .store
            .revoke(&id, input.revision, state.http.auth.username())
            .await
    };
    match result {
        Ok(record) => {
            state.http.sessions.disconnect(&id).await;
            Json(view(&state, record).await).into_response()
        }
        Err(error) => error.into_response(),
    }
}

async fn events(State(state): State<IdentityState>, Path(id): Path<String>) -> Response {
    match state.store.events(&id).await {
        Ok(events) => Json(events).into_response(),
        Err(error) => error.into_response(),
    }
}

#[cfg(test)]
#[path = "identity_http_tests.rs"]
mod tests;
