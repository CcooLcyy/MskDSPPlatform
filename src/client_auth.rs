use crate::accounts::{AccountError, AccountRecord, AccountStore};
use serde::Serialize;
use std::{
    collections::HashMap,
    sync::Arc,
    time::{Duration, Instant},
};
use thiserror::Error;
use tokio::sync::{Mutex, Semaphore};
use uuid::Uuid;

const MAX_SESSIONS: usize = 1024;
const MAX_ACCOUNT_SESSIONS: usize = 16;

#[derive(Clone)]
pub struct ClientAuth(Arc<ClientAuthInner>);

struct ClientAuthInner {
    store: AccountStore,
    session_seconds: u64,
    sessions: Mutex<HashMap<String, SessionEntry>>,
    attempts: Mutex<(Instant, u32)>,
    verification_slots: Arc<Semaphore>,
}

#[derive(Clone)]
struct SessionEntry {
    account_id: String,
    auth_version: i64,
    expires_at: Instant,
}

#[derive(Debug, Clone, Serialize)]
pub struct LoginResponse {
    pub access_token: String,
    pub token_type: &'static str,
    pub expires_in: u64,
    pub account: AccountRecord,
}

#[derive(Debug, Clone)]
pub struct ClientSession {
    pub account: AccountRecord,
    pub expires_in: u64,
}

#[derive(Debug, Error)]
pub enum ClientAuthError {
    #[error("{0}")]
    Invalid(String),
    #[error("用户名、密码或登录状态无效")]
    Unauthorized,
    #[error("登录尝试过于频繁，请一分钟后重试")]
    RateLimited,
    #[error("登录请求繁忙，请稍后重试")]
    Busy,
    #[error("当前登录会话过多，请退出其他登录后重试")]
    SessionLimit,
    #[error("账号存储暂不可用，请稍后重试")]
    Storage,
}

impl ClientAuth {
    pub fn new(store: AccountStore, session_seconds: u64) -> Self {
        Self(Arc::new(ClientAuthInner {
            store,
            session_seconds: session_seconds.max(1),
            sessions: Mutex::new(HashMap::new()),
            attempts: Mutex::new((Instant::now(), 0)),
            verification_slots: Arc::new(Semaphore::new(2)),
        }))
    }

    pub async fn login(
        &self,
        username: &str,
        password: &str,
    ) -> Result<LoginResponse, ClientAuthError> {
        let result = self.login_inner(username, password).await;
        match &result {
            Ok(login) => tracing::info!(account_id = %login.account.id, "上位机账号登录成功"),
            Err(error) => {
                let code = match error {
                    ClientAuthError::Invalid(_) => "invalid",
                    ClientAuthError::Unauthorized => "unauthorized",
                    ClientAuthError::RateLimited => "rate_limited",
                    ClientAuthError::Busy => "busy",
                    ClientAuthError::SessionLimit => "session_limit",
                    ClientAuthError::Storage => "storage",
                };
                tracing::warn!(code, "上位机账号登录失败");
            }
        }
        result
    }

    async fn login_inner(
        &self,
        username: &str,
        password: &str,
    ) -> Result<LoginResponse, ClientAuthError> {
        if username.is_empty()
            || username.len() > 128
            || password.is_empty()
            || password.len() > 128
        {
            return Err(ClientAuthError::Invalid("用户名或密码长度不正确".into()));
        }
        {
            let mut attempts = self.0.attempts.lock().await;
            if attempts.0.elapsed() >= Duration::from_secs(60) {
                *attempts = (Instant::now(), 0);
            }
            if attempts.1 >= 10 {
                return Err(ClientAuthError::RateLimited);
            }
            attempts.1 += 1;
        }
        let permit = self
            .0
            .verification_slots
            .clone()
            .try_acquire_owned()
            .map_err(|_| ClientAuthError::Busy)?;
        let store = self.0.store.clone();
        let username = username.to_owned();
        let password = password.to_owned();
        // 请求取消不释放仍在执行的密码验证许可，限制后台哈希工作的实际并发数。
        let account = tokio::spawn(async move {
            let _permit = permit;
            store.verify(&username, &password).await
        })
        .await
        .map_err(|_| ClientAuthError::Storage)?
        .map_err(account_error)?;
        let now = Instant::now();
        let mut sessions = self.0.sessions.lock().await;
        sessions.retain(|_, session| {
            session.expires_at > now
                && (session.account_id != account.id
                    || session.auth_version == account.auth_version)
        });
        if sessions.len() >= MAX_SESSIONS
            || sessions
                .values()
                .filter(|entry| entry.account_id == account.id)
                .count()
                >= MAX_ACCOUNT_SESSIONS
        {
            return Err(ClientAuthError::SessionLimit);
        }
        let account = self
            .0
            .store
            .touch_login(&account.id, account.auth_version)
            .await
            .map_err(account_error)?;
        let token = loop {
            let candidate = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
            if !sessions.contains_key(&candidate) {
                break candidate;
            }
        };
        sessions.insert(
            token.clone(),
            SessionEntry {
                account_id: account.id.clone(),
                auth_version: account.auth_version,
                expires_at: now + Duration::from_secs(self.0.session_seconds),
            },
        );
        Ok(LoginResponse {
            access_token: token,
            token_type: "Bearer",
            expires_in: self.0.session_seconds,
            account,
        })
    }

    pub async fn authenticate(&self, token: &str) -> Result<ClientSession, ClientAuthError> {
        if token.len() != 64 || !token.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err(ClientAuthError::Unauthorized);
        }
        let entry = {
            let mut sessions = self.0.sessions.lock().await;
            let now = Instant::now();
            sessions.retain(|_, entry| entry.expires_at > now);
            sessions
                .get(token)
                .cloned()
                .ok_or(ClientAuthError::Unauthorized)?
        };
        let account = match self
            .0
            .store
            .check_authorization(&entry.account_id, entry.auth_version)
            .await
        {
            Ok(account) => account,
            Err(AccountError::Storage) => return Err(ClientAuthError::Storage),
            Err(_) => {
                self.0.sessions.lock().await.remove(token);
                return Err(ClientAuthError::Unauthorized);
            }
        };
        // 数据库校验期间发生退出或过期时，不能继续使用之前读取的会话。
        let mut sessions = self.0.sessions.lock().await;
        let now = Instant::now();
        sessions.retain(|_, entry| entry.expires_at > now);
        let current = sessions.get(token).ok_or(ClientAuthError::Unauthorized)?;
        Ok(ClientSession {
            account,
            expires_in: current.expires_at.saturating_duration_since(now).as_secs(),
        })
    }

    /// 仅测试使用：保留会话记录并令其过期，继续验证真实过期检查与连接轮询。
    #[cfg(test)]
    pub(crate) async fn expire_session_for_test(&self, token: &str) -> bool {
        let mut sessions = self.0.sessions.lock().await;
        let Some(session) = sessions.get_mut(token) else {
            return false;
        };
        session.expires_at = Instant::now();
        true
    }

    pub async fn logout(&self, token: &str) {
        if let Some(entry) = self.0.sessions.lock().await.remove(token) {
            tracing::info!(account_id = %entry.account_id, "上位机账号已退出登录");
        }
    }

    pub async fn revoke_account(&self, account_id: &str) {
        self.0
            .sessions
            .lock()
            .await
            .retain(|_, entry| entry.account_id != account_id);
        tracing::info!(account_id, "上位机账号登录会话已撤销");
    }
}

fn account_error(error: AccountError) -> ClientAuthError {
    match error {
        AccountError::Storage => ClientAuthError::Storage,
        _ => ClientAuthError::Unauthorized,
    }
}

#[cfg(test)]
#[path = "client_auth_tests.rs"]
mod tests;
