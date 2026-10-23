//! 管理员设备管理 API；凭据仅创建或轮换时返回，不进入设备列表。
use super::{hub::Hub, store::*};
use crate::{accounts::AccountStore, auth, http::HttpState};
use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, Path, Request, State, rejection::JsonRejection},
    http::{HeaderMap, StatusCode},
    middleware::{self, Next},
    response::{Html, IntoResponse, Response},
    routing::{get, post},
};
use serde::{Deserialize, Serialize};
#[derive(Clone)]
struct DeviceState {
    http: HttpState,
    store: DeviceStore,
    accounts: AccountStore,
    hub: Hub,
}
pub fn router(http: HttpState, store: DeviceStore, accounts: AccountStore, hub: Hub) -> Router {
    let state = DeviceState {
        http,
        store,
        accounts,
        hub,
    };
    Router::new()
        .route("/devices", get(page))
        .route_layer(middleware::from_fn_with_state(
            state.http.clone(),
            auth::require_admin,
        ))
        .merge(
            Router::new()
                .route("/api/v1/devices", get(list).post(create))
                .route(
                    "/api/v1/devices/{id}",
                    get(detail).put(update).delete(delete),
                )
                .route("/api/v1/devices/{id}/grants", get(grants).put(set_grants))
                .route("/api/v1/devices/{id}/token", post(rotate))
                .route_layer(middleware::from_fn_with_state(state.clone(), require_admin)),
        )
        .layer(DefaultBodyLimit::max(256 * 1024))
        .layer(middleware::from_fn(auth::no_cache))
        .with_state(state)
}
async fn page() -> Html<&'static str> {
    Html(include_str!("../../web/devices.html"))
}
async fn require_admin(State(state): State<DeviceState>, request: Request, next: Next) -> Response {
    let identity = auth::me(State(state.http), request.headers().clone()).await;
    if identity.status() == StatusCode::OK {
        next.run(request).await
    } else {
        identity
    }
}
fn error(code: StatusCode, message: impl ToString) -> Response {
    (code, Json(serde_json::json!({"error":message.to_string()}))).into_response()
}
impl IntoResponse for StoreError {
    fn into_response(self) -> Response {
        let code = match self {
            Self::Invalid(_) => StatusCode::BAD_REQUEST,
            Self::NotFound => StatusCode::NOT_FOUND,
            Self::Conflict => StatusCode::CONFLICT,
            Self::Denied => StatusCode::FORBIDDEN,
            Self::Storage => StatusCode::SERVICE_UNAVAILABLE,
        };
        error(code, self)
    }
}
struct ApiError(StatusCode, &'static str);
impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        error(self.0, self.1)
    }
}
fn action(headers: &HeaderMap) -> Result<(), ApiError> {
    if auth::action_allowed_for(headers, "device") {
        Ok(())
    } else {
        Err(ApiError(
            StatusCode::FORBIDDEN,
            "缺少操作标记或请求来源不正确",
        ))
    }
}
fn body<T>(value: Result<Json<T>, JsonRejection>) -> Result<T, ApiError> {
    value
        .map(|Json(v)| v)
        .map_err(|e| ApiError(e.status(), "设备请求格式不正确或超过大小限制"))
}
#[derive(Serialize)]
struct View {
    #[serde(flatten)]
    device: DeviceRecord,
    online: bool,
    services: Vec<String>,
    agent_version: String,
}
async fn view(state: &DeviceState, device: DeviceRecord) -> View {
    let live = state.hub.snapshot(&device.device_id).await;
    View {
        device,
        online: live.is_some(),
        services: live
            .as_ref()
            .map(|d| d.services.clone())
            .unwrap_or_default(),
        agent_version: live.map(|d| d.agent_version).unwrap_or_default(),
    }
}
async fn list(State(state): State<DeviceState>) -> Response {
    match state.store.list().await {
        Ok(devices) => {
            let mut result = Vec::with_capacity(devices.len());
            for device in devices {
                result.push(view(&state, device).await);
            }
            Json(result).into_response()
        }
        Err(e) => e.into_response(),
    }
}
async fn detail(State(state): State<DeviceState>, Path(id): Path<String>) -> Response {
    match state.store.get(&id).await {
        Ok(device) => Json(view(&state, device).await).into_response(),
        Err(e) => e.into_response(),
    }
}
async fn create(
    State(state): State<DeviceState>,
    headers: HeaderMap,
    input: Result<Json<NewDevice>, JsonRejection>,
) -> Response {
    if let Err(e) = action(&headers) {
        return e.into_response();
    }
    let input = match body(input) {
        Ok(v) => v,
        Err(e) => return e.into_response(),
    };
    // 普通账号必须逐台明确授权，管理员通过角色自动获得设备访问权限。
    match state.store.create(input, vec![]).await {
        Ok(created) => {
            tracing::info!(actor=%state.http.auth.username(),device_id=%created.device.device_id,"管理员已创建设备，普通账号授权默认为空");
            (StatusCode::CREATED, Json(created)).into_response()
        }
        Err(e) => e.into_response(),
    }
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct DeviceUpdateInput {
    display_name: String,
    enabled: bool,
    #[serde(default)]
    grants: Option<Vec<Grant>>,
}
async fn update(
    State(state): State<DeviceState>,
    Path(id): Path<String>,
    headers: HeaderMap,
    input: Result<Json<DeviceUpdateInput>, JsonRejection>,
) -> Response {
    if let Err(e) = action(&headers) {
        return e.into_response();
    }
    let input = match body(input) {
        Ok(v) => v,
        Err(e) => return e.into_response(),
    };
    if let Some(grants) = &input.grants {
        if let Err(response) = validate_accounts(&state.accounts, grants).await {
            return response;
        }
    }
    let enabled = input.enabled;
    let settings = DeviceUpdate {
        display_name: input.display_name,
        enabled,
    };
    let result = match input.grants {
        Some(grants) => {
            state
                .store
                .update_with_grants(&id, settings, Some(grants))
                .await
        }
        None => state.store.update(&id, settings).await,
    };
    match result {
        Ok(device) => {
            if !enabled {
                state.hub.disconnect(&id).await;
            }
            tracing::info!(actor=%state.http.auth.username(),device_id=%id,enabled,"管理员已更新设备");
            Json(view(&state, device).await).into_response()
        }
        Err(e) => e.into_response(),
    }
}
async fn delete(
    State(state): State<DeviceState>,
    Path(id): Path<String>,
    headers: HeaderMap,
) -> Response {
    if let Err(e) = action(&headers) {
        return e.into_response();
    }
    match state.store.delete(&id).await {
        Ok(()) => {
            state.hub.disconnect(&id).await;
            tracing::info!(actor=%state.http.auth.username(),device_id=%id,"管理员已删除设备");
            StatusCode::NO_CONTENT.into_response()
        }
        Err(e) => e.into_response(),
    }
}
async fn rotate(
    State(state): State<DeviceState>,
    Path(id): Path<String>,
    headers: HeaderMap,
) -> Response {
    if let Err(e) = action(&headers) {
        return e.into_response();
    }
    match state.store.rotate(&id).await {
        Ok(created) => {
            state.hub.disconnect(&id).await;
            tracing::info!(actor=%state.http.auth.username(),device_id=%id,"管理员已轮换设备凭据");
            Json(created).into_response()
        }
        Err(e) => e.into_response(),
    }
}
async fn grants(State(state): State<DeviceState>, Path(id): Path<String>) -> Response {
    match state.store.grants(&id).await {
        Ok(value) => Json(value).into_response(),
        Err(e) => e.into_response(),
    }
}
async fn set_grants(
    State(state): State<DeviceState>,
    Path(id): Path<String>,
    headers: HeaderMap,
    input: Result<Json<Vec<Grant>>, JsonRejection>,
) -> Response {
    if let Err(e) = action(&headers) {
        return e.into_response();
    }
    let input = match body(input) {
        Ok(v) => v,
        Err(e) => return e.into_response(),
    };
    if let Err(response) = validate_accounts(&state.accounts, &input).await {
        return response;
    }
    match state.store.set_grants(&id, input).await {
        Ok(()) => {
            tracing::info!(actor=%state.http.auth.username(),device_id=%id,"管理员已更新设备账号授权");
            StatusCode::NO_CONTENT.into_response()
        }
        Err(e) => e.into_response(),
    }
}
async fn validate_accounts(accounts: &AccountStore, grants: &[Grant]) -> Result<(), Response> {
    for grant in grants {
        match accounts.get(&grant.account_id).await {
            Ok(_) => {}
            Err(crate::accounts::AccountError::NotFound) => {
                return Err(error(StatusCode::BAD_REQUEST, "授权账号不存在"));
            }
            Err(_) => {
                return Err(error(StatusCode::SERVICE_UNAVAILABLE, "账号存储暂不可用"));
            }
        }
    }
    Ok(())
}
#[cfg(test)]
#[path = "http_tests.rs"]
mod tests;
