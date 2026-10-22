use crate::accounts::{AccountError, AccountRecord, AccountStore};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::{
    sync::Arc,
    time::{Duration, Instant},
};
use thiserror::Error;
use tokio::sync::{Mutex, Semaphore};
use uuid::Uuid;

#[derive(Clone)]
pub struct ClientAuth(Arc<ClientAuthInner>);

struct ClientAuthInner {
    store: AccountStore,
    session_seconds: u64,
    attempts: Mutex<(Instant, u32)>,
    verification_slots: Arc<Semaphore>,
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
        let token = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
        let account = self
            .0
            .store
            .create_client_session(
                &account.id,
                account.auth_version,
                token_digest(&token),
                self.0.session_seconds,
            )
            .await
            .map_err(account_error)?
            .ok_or(ClientAuthError::SessionLimit)?;
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
        let (account, expires_in) = self
            .0
            .store
            .authenticate_client_session(token_digest(token))
            .await
            .map_err(account_error)?;
        Ok(ClientSession {
            account,
            expires_in,
        })
    }

    /// 仅测试使用：保留会话记录并令其过期，继续验证真实过期检查与连接轮询。
    #[cfg(test)]
    pub(crate) async fn expire_session_for_test(&self, token: &str) -> bool {
        self.0
            .store
            .expire_client_session(token_digest(token))
            .await
            .unwrap()
    }

    pub async fn logout(&self, token: &str) -> Result<(), ClientAuthError> {
        self.0
            .store
            .delete_client_session(token_digest(token))
            .await
            .map_err(account_error)?;
        tracing::info!("上位机登录会话已退出");
        Ok(())
    }

    pub async fn revoke_account(&self, account_id: &str) {
        match self.0.store.delete_account_sessions(account_id).await {
            Ok(()) => tracing::info!(account_id, "上位机账号登录会话已撤销"),
            // 管理接口已持久化认证版本，清理失败仍会在认证时拒绝全部旧会话。
            Err(_) => tracing::warn!(account_id, "清理上位机会话失败，旧会话仍由账号认证版本拒绝"),
        }
    }
}

fn token_digest(token: &str) -> String {
    format!("{:x}", Sha256::digest(token.as_bytes()))
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

#[cfg(test)]
#[path = "client_auth_persistence_tests.rs"]
mod persistence_tests;
