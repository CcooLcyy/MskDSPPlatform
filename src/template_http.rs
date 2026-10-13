use crate::{
    auth,
    http::HttpState,
    modbus_templates::{StoreError, TemplateStore},
};
use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, Path, Request, State, rejection::JsonRejection},
    http::{HeaderMap, StatusCode, header},
    middleware::{self, Next},
    response::{Html, IntoResponse, Response},
    routing::get,
};
use serde_json::{Value, json};
use tracing::{info, warn};

const LIMIT: usize = 10 * 1024 * 1024;

#[derive(Clone)]
struct TemplateState {
    auth: HttpState,
    templates: TemplateStore,
}

pub fn router(auth: HttpState, templates: TemplateStore) -> Router {
    let state = TemplateState { auth, templates };
    Router::new()
        .route("/api/v1/modbus-templates", get(list).post(create))
        .route(
            "/api/v1/modbus-templates/{id}",
            get(detail).put(update).delete(delete),
        )
        .route("/api/v1/modbus-templates/{id}/download", get(download))
        .layer(DefaultBodyLimit::max(LIMIT))
        .route_layer(middleware::from_fn_with_state(state.clone(), require_admin))
        .layer(middleware::from_fn(auth::no_cache))
        .with_state(state)
}

pub async fn page() -> Html<&'static str> {
    Html(include_str!("../web/modbus-templates.html"))
}

async fn require_admin(
    State(state): State<TemplateState>,
    request: Request,
    next: Next,
) -> Response {
    let identity = auth::me(State(state.auth), request.headers().clone()).await;
    if identity.status() == StatusCode::OK {
        next.run(request).await
    } else {
        identity
    }
}

fn error(status: StatusCode, message: impl ToString) -> Response {
    (status, Json(json!({"error": message.to_string()}))).into_response()
}

fn storage_error(failure: StoreError) -> Response {
    let status = failure.status();
    warn!(status = status.as_u16(), error = %failure, "点表操作失败");
    error(status, failure)
}

struct RequestError {
    status: StatusCode,
    message: &'static str,
}

impl IntoResponse for RequestError {
    fn into_response(self) -> Response {
        error(self.status, self.message)
    }
}

fn request_error(status: StatusCode, message: &'static str) -> RequestError {
    RequestError { status, message }
}

fn action(headers: &HeaderMap) -> Result<(), RequestError> {
    let actions: Vec<_> = headers.get_all("X-MskDSP-Action").iter().collect();
    if actions.len() != 1 || actions[0] != "modbus-template" {
        return Err(request_error(StatusCode::FORBIDDEN, "缺少点表操作标记"));
    }
    let hosts: Vec<_> = headers.get_all(header::HOST).iter().collect();
    let origins: Vec<_> = headers.get_all(header::ORIGIN).iter().collect();
    if hosts.len() != 1 || origins.len() > 1 {
        return Err(request_error(StatusCode::FORBIDDEN, "点表请求来源不正确"));
    }
    let host = hosts[0].to_str().unwrap_or_default();
    if host.contains('@')
        || host.parse::<axum::http::uri::Authority>().is_err()
        || (!origins.is_empty()
            && !origins[0].to_str().is_ok_and(|origin| {
                origin.eq_ignore_ascii_case(&format!("http://{host}"))
                    || origin.eq_ignore_ascii_case(&format!("https://{host}"))
            }))
    {
        return Err(request_error(StatusCode::FORBIDDEN, "不允许跨源点表操作"));
    }
    Ok(())
}

fn revision(headers: &HeaderMap) -> Result<u64, RequestError> {
    let values: Vec<_> = headers.get_all(header::IF_MATCH).iter().collect();
    if values.is_empty() {
        return Err(request_error(
            StatusCode::PRECONDITION_REQUIRED,
            "请提供点表版本后再保存或删除",
        ));
    }
    if values.len() != 1 {
        return Err(request_error(StatusCode::BAD_REQUEST, "点表版本格式不正确"));
    }
    values[0]
        .to_str()
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .filter(|v| *v > 0)
        .ok_or_else(|| request_error(StatusCode::BAD_REQUEST, "点表版本格式不正确"))
}

fn body(value: Result<Json<Value>, JsonRejection>) -> Result<Value, RequestError> {
    value.map(|Json(value)| value).map_err(|rejection| {
        request_error(
            rejection.status(),
            if rejection.status() == StatusCode::PAYLOAD_TOO_LARGE {
                "点表文件不能超过 10 MiB"
            } else {
                "点表请求必须是合法 JSON，内容类型为 application/json"
            },
        )
    })
}

async fn list(State(state): State<TemplateState>) -> Response {
    match state.templates.list().await {
        Ok(records) => Json(records).into_response(),
        Err(failure) => storage_error(failure),
    }
}

async fn detail(State(state): State<TemplateState>, Path(id): Path<String>) -> Response {
    match state.templates.get(&id).await {
        Ok(record) => Json(record).into_response(),
        Err(failure) => storage_error(failure),
    }
}

async fn create(
    State(state): State<TemplateState>,
    headers: HeaderMap,
    value: Result<Json<Value>, JsonRejection>,
) -> Response {
    if let Err(response) = action(&headers) {
        return response.into_response();
    }
    let template = match body(value) {
        Ok(value) => value,
        Err(response) => return response.into_response(),
    };
    match state.templates.create(template).await {
        Ok(record) => {
            info!(id = %record["id"], points = %record["template"]["points"].as_array().map_or(0, Vec::len), "Modbus 点表已创建并保存");
            (StatusCode::CREATED, Json(record)).into_response()
        }
        Err(failure) => storage_error(failure),
    }
}

async fn update(
    State(state): State<TemplateState>,
    Path(id): Path<String>,
    headers: HeaderMap,
    value: Result<Json<Value>, JsonRejection>,
) -> Response {
    if let Err(response) = action(&headers) {
        return response.into_response();
    }
    let revision = match revision(&headers) {
        Ok(value) => value,
        Err(response) => return response.into_response(),
    };
    let template = match body(value) {
        Ok(value) => value,
        Err(response) => return response.into_response(),
    };
    match state.templates.update(&id, revision, template).await {
        Ok(record) => {
            info!(id = %id, revision = %record["revision"], "Modbus 点表修改已保存");
            Json(record).into_response()
        }
        Err(failure) => storage_error(failure),
    }
}

async fn delete(
    State(state): State<TemplateState>,
    Path(id): Path<String>,
    headers: HeaderMap,
) -> Response {
    if let Err(response) = action(&headers) {
        return response.into_response();
    }
    let revision = match revision(&headers) {
        Ok(value) => value,
        Err(response) => return response.into_response(),
    };
    match state.templates.delete(&id, revision).await {
        Ok(()) => {
            info!(id = %id, revision, "Modbus 点表已删除");
            StatusCode::NO_CONTENT.into_response()
        }
        Err(failure) => storage_error(failure),
    }
}

async fn download(State(state): State<TemplateState>, Path(id): Path<String>) -> Response {
    match state.templates.get(&id).await {
        Ok(record) => {
            // 模板 id 经过白名单校验，文件名不会包含路径或响应头控制字符。
            let filename = record["template"]["id"]
                .as_str()
                .unwrap_or("modbus-template");
            let attachment = format!("attachment; filename=\"{filename}.json\"");
            match serde_json::to_string_pretty(&record["template"]) {
                Ok(json) => (
                    [
                        (header::CONTENT_TYPE, "application/json; charset=utf-8"),
                        (header::CONTENT_DISPOSITION, attachment.as_str()),
                    ],
                    json + "\n",
                )
                    .into_response(),
                Err(_) => error(StatusCode::INTERNAL_SERVER_ERROR, "点表导出失败"),
            }
        }
        Err(failure) => storage_error(failure),
    }
}
