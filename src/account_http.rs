use crate::{
    accounts::{AccountError, AccountRecord, AccountStore, AccountUpdate, NewAccount},
    auth,
    client_auth::ClientAuth,
    http::HttpState,
};
use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, Path, Request, State, rejection::JsonRejection},
    http::{HeaderMap, StatusCode},
    middleware::{self, Next},
    response::{Html, IntoResponse, Redirect, Response},
    routing::{get, post},
};
use serde::{Deserialize, Serialize};
use serde_json::json;

#[derive(Clone)]
struct AccountState {
    http: HttpState,
    accounts: AccountStore,
    clients: ClientAuth,
}

pub fn router(http: HttpState, accounts: AccountStore, clients: ClientAuth) -> Router {
    let state = AccountState {
        http,
        accounts,
        clients,
    };
    Router::new()
        .route("/accounts", get(page))
        .route("/agents", get(|| async { Redirect::to("/accounts") }))
        .route_layer(middleware::from_fn_with_state(
            state.http.clone(),
            auth::require_admin,
        ))
        .merge(
            Router::new()
                .route("/api/v1/accounts", get(list).post(create))
                .route("/api/v1/accounts/{id}", get(detail).put(update))
                .route("/api/v1/accounts/{id}/password", post(reset_password))
                .route_layer(middleware::from_fn_with_state(state.clone(), require_admin)),
        )
        .layer(DefaultBodyLimit::max(8192))
        .layer(middleware::from_fn(auth::no_cache))
        .with_state(state)
}

async fn page() -> Html<&'static str> {
    Html(include_str!("../web/accounts.html"))
}

async fn require_admin(
    State(state): State<AccountState>,
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

impl IntoResponse for AccountError {
    fn into_response(self) -> Response {
        let status = match self {
            Self::Invalid(_) => StatusCode::BAD_REQUEST,
            Self::NotFound => StatusCode::NOT_FOUND,
            Self::Conflict | Self::DuplicateUsername => StatusCode::CONFLICT,
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

fn action(headers: &HeaderMap, expected: &str) -> Result<(), RequestError> {
    if auth::action_allowed_for(headers, expected) {
        Ok(())
    } else {
        Err(RequestError(
            StatusCode::FORBIDDEN,
            "缺少操作标记或请求来源不正确",
        ))
    }
}

fn body<T>(value: Result<Json<T>, JsonRejection>) -> Result<T, RequestError> {
    value
        .map(|Json(value)| value)
        .map_err(|rejection| RequestError(rejection.status(), "账号请求格式不正确或超过 8 KiB"))
}

#[derive(Serialize)]
struct AccountView {
    #[serde(flatten)]
    account: AccountRecord,
    online_connections: usize,
}

async fn view(state: &AccountState, account: AccountRecord) -> AccountView {
    let online_connections = if account.enabled {
        state.http.sessions.count_for_account(&account.id).await
    } else {
        0
    };
    AccountView {
        account,
        online_connections,
    }
}

async fn list(State(state): State<AccountState>) -> Response {
    match state.accounts.list().await {
        Ok(accounts) => {
            let mut result = Vec::with_capacity(accounts.len());
            for account in accounts {
                result.push(view(&state, account).await);
            }
            Json(result).into_response()
        }
        Err(error) => error.into_response(),
    }
}

async fn detail(State(state): State<AccountState>, Path(id): Path<String>) -> Response {
    match state.accounts.get(&id).await {
        Ok(account) => Json(view(&state, account).await).into_response(),
        Err(error) => error.into_response(),
    }
}

async fn create(
    State(state): State<AccountState>,
    headers: HeaderMap,
    value: Result<Json<NewAccount>, JsonRejection>,
) -> Response {
    if let Err(error) = action(&headers, "account") {
        return error.into_response();
    }
    let input = match body(value) {
        Ok(value) => value,
        Err(error) => return error.into_response(),
    };
    if input
        .username
        .trim()
        .eq_ignore_ascii_case(state.http.auth.username())
    {
        return error(StatusCode::BAD_REQUEST, "该用户名为平台管理员保留名称");
    }
    match state.accounts.create(input).await {
        Ok(account) => {
            tracing::info!(actor=%state.http.auth.username(),account_id=%account.id,"管理员已创建上位机登录账号");
            (StatusCode::CREATED, Json(view(&state, account).await)).into_response()
        }
        Err(error) => error.into_response(),
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct UpdateRequest {
    revision: i64,
    display_name: String,
    enabled: bool,
}

async fn update(
    State(state): State<AccountState>,
    Path(id): Path<String>,
    headers: HeaderMap,
    value: Result<Json<UpdateRequest>, JsonRejection>,
) -> Response {
    if let Err(error) = action(&headers, "account") {
        return error.into_response();
    }
    let input = match body(value) {
        Ok(value) => value,
        Err(error) => return error.into_response(),
    };
    match state
        .accounts
        .update(
            &id,
            input.revision,
            AccountUpdate {
                display_name: input.display_name,
                enabled: input.enabled,
            },
        )
        .await
    {
        Ok(account) => {
            if !account.enabled {
                state.clients.revoke_account(&account.id).await;
                state.http.sessions.disconnect_account(&account.id).await;
            }
            tracing::info!(actor=%state.http.auth.username(),account_id=%account.id,enabled=account.enabled,"管理员已更新上位机登录账号");
            Json(view(&state, account).await).into_response()
        }
        Err(error) => error.into_response(),
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PasswordRequest {
    revision: i64,
    password: String,
}

async fn reset_password(
    State(state): State<AccountState>,
    Path(id): Path<String>,
    headers: HeaderMap,
    value: Result<Json<PasswordRequest>, JsonRejection>,
) -> Response {
    if let Err(error) = action(&headers, "account") {
        return error.into_response();
    }
    let input = match body(value) {
        Ok(value) => value,
        Err(error) => return error.into_response(),
    };
    match state
        .accounts
        .reset_password(&id, input.revision, input.password)
        .await
    {
        Ok(account) => {
            state.clients.revoke_account(&account.id).await;
            state.http.sessions.disconnect_account(&account.id).await;
            tracing::info!(actor=%state.http.auth.username(),account_id=%account.id,"管理员已重置上位机登录密码，旧会话已失效");
            Json(view(&state, account).await).into_response()
        }
        Err(error) => error.into_response(),
    }
}

#[cfg(test)]
#[path = "account_http_tests.rs"]
mod tests;
