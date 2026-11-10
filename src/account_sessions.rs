//! 上位机持久会话；只存令牌摘要，与账号认证版本在同一数据库事务内校验。
use super::{AccountError, AccountRecord, AccountStore, get_record, storage_error};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};

pub(crate) const MAX_SESSIONS: usize = 1024;
pub(crate) const MAX_ACCOUNT_SESSIONS: usize = 16;
const SESSION_IDLE_TIMEOUT_MS: i64 = 120_000;

pub(super) fn initialize(connection: &mut Connection) -> Result<(), AccountError> {
    // 辅助表独立命名，不提升账号库版本，保留旧程序读取账号的兼容性。
    let transaction = connection
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(storage_error)?;
    transaction
        .execute_batch(
            "CREATE TABLE IF NOT EXISTS client_sessions_v1 (
            token_hash TEXT PRIMARY KEY NOT NULL CHECK(length(token_hash)=64),
            account_id TEXT NOT NULL,
            auth_version INTEGER NOT NULL CHECK(auth_version>0),
            issued_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL CHECK(expires_at>issued_at),
            last_seen_at INTEGER NOT NULL DEFAULT 0
         );
         CREATE INDEX IF NOT EXISTS client_sessions_v1_account ON client_sessions_v1(account_id);
         CREATE INDEX IF NOT EXISTS client_sessions_v1_expiry ON client_sessions_v1(expires_at);",
        )
        .map_err(storage_error)?;
    let has_last_seen: bool = transaction
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM pragma_table_info('client_sessions_v1') WHERE name='last_seen_at')",
            [],
            |row| row.get(0),
        )
        .map_err(storage_error)?;
    if !has_last_seen {
        transaction
            .execute(
                "ALTER TABLE client_sessions_v1 ADD COLUMN last_seen_at INTEGER NOT NULL DEFAULT 0",
                [],
            )
            .map_err(storage_error)?;
    }
    transaction.prepare("SELECT token_hash,account_id,auth_version,issued_at,expires_at,last_seen_at FROM client_sessions_v1 LIMIT 0").map_err(storage_error)?;
    // 旧版会话缺少心跳时间，升级后给一次重连窗口。之后重启保留原时间。
    transaction
        .execute(
            "UPDATE client_sessions_v1 SET last_seen_at=?1 WHERE last_seen_at=0",
            [chrono::Utc::now().timestamp_millis()],
        )
        .map_err(storage_error)?;
    transaction.commit().map_err(storage_error)?;
    tracing::info!("上位机持久登录会话存储已就绪");
    Ok(())
}

impl AccountStore {
    /// 返回空值表示达到会话容量；事务失败不能留下登录时间或部分会话。
    pub(crate) async fn create_client_session(
        &self,
        account_id: &str,
        auth_version: i64,
        token_hash: String,
        session_seconds: u64,
    ) -> Result<Option<AccountRecord>, AccountError> {
        let account_id = account_id.to_owned();
        self.blocking(move |connection| {
            let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate).map_err(storage_error)?;
            let now = chrono::Utc::now().timestamp_millis();
            purge_inactive(&transaction, now)?;
            let account = get_record(&transaction, &account_id)?;
            if !account.enabled || account.auth_version != auth_version {
                return Err(AccountError::Unauthorized);
            }
            let (total, owned): (i64, i64) = transaction.query_row(
                "SELECT count(*),coalesce(sum(account_id=?1),0) FROM client_sessions_v1",
                [&account_id], |row| Ok((row.get(0)?, row.get(1)?))
            ).map_err(storage_error)?;
            if total >= MAX_SESSIONS as i64 || owned >= MAX_ACCOUNT_SESSIONS as i64 {
                transaction.commit().map_err(storage_error)?;
                return Ok(None);
            }
            let duration = i64::try_from(session_seconds).unwrap_or(i64::MAX).saturating_mul(1000);
            transaction.execute(
                "INSERT INTO client_sessions_v1(token_hash,account_id,auth_version,issued_at,expires_at,last_seen_at) VALUES (?1,?2,?3,?4,?5,?4)",
                params![token_hash, account_id, auth_version, now, now.saturating_add(duration)]
            ).map_err(storage_error)?;
            transaction.execute("UPDATE accounts SET last_login_at=?1 WHERE id=?2", params![now, account_id]).map_err(storage_error)?;
            let account = get_record(&transaction, &account_id)?;
            transaction.commit().map_err(storage_error)?;
            Ok(Some(account))
        }).await
    }

    pub(crate) async fn authenticate_client_session(
        &self,
        token_hash: String,
    ) -> Result<(AccountRecord, u64), AccountError> {
        self.check_client_session(token_hash, false).await
    }

    pub(crate) async fn refresh_client_session(
        &self,
        token_hash: String,
    ) -> Result<(AccountRecord, u64), AccountError> {
        self.check_client_session(token_hash, true).await
    }

    async fn check_client_session(
        &self,
        token_hash: String,
        refresh: bool,
    ) -> Result<(AccountRecord, u64), AccountError> {
        self.blocking(move |connection| {
            let behavior = if refresh {
                TransactionBehavior::Immediate
            } else {
                TransactionBehavior::Deferred
            };
            let transaction = connection.transaction_with_behavior(behavior).map_err(storage_error)?;
            let entry: Option<(String, i64, i64, i64)> = transaction.query_row(
                "SELECT account_id,auth_version,expires_at,last_seen_at FROM client_sessions_v1 WHERE token_hash=?1",
                [&token_hash], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?))
            ).optional().map_err(storage_error)?;
            let (account_id, auth_version, expires_at, last_seen_at) = entry.ok_or(AccountError::Unauthorized)?;
            let account = match get_record(&transaction, &account_id) {
                Err(AccountError::NotFound) => return Err(AccountError::Unauthorized),
                result => result?,
            };
            let now = chrono::Utc::now().timestamp_millis();
            if expires_at <= now || last_seen_at <= now - SESSION_IDLE_TIMEOUT_MS
                || !account.enabled || account.auth_version != auth_version {
                return Err(AccountError::Unauthorized);
            }
            if refresh {
                transaction.execute(
                    "UPDATE client_sessions_v1 SET last_seen_at=max(last_seen_at,?1) WHERE token_hash=?2",
                    params![now, token_hash],
                ).map_err(storage_error)?;
            }
            transaction.commit().map_err(storage_error)?;
            Ok((account, ((expires_at - now) / 1000) as u64))
        }).await
    }

    pub(crate) async fn purge_inactive_client_sessions(&self) -> Result<usize, AccountError> {
        self.blocking(move |connection| {
            purge_inactive(connection, chrono::Utc::now().timestamp_millis())
        })
        .await
    }

    pub(crate) async fn delete_client_session(
        &self,
        token_hash: String,
    ) -> Result<(), AccountError> {
        self.blocking(move |connection| {
            connection
                .execute(
                    "DELETE FROM client_sessions_v1 WHERE token_hash=?1",
                    [token_hash],
                )
                .map_err(storage_error)?;
            Ok(())
        })
        .await
    }

    pub(crate) async fn delete_account_sessions(
        &self,
        account_id: &str,
    ) -> Result<(), AccountError> {
        let account_id = account_id.to_owned();
        self.blocking(move |connection| {
            connection
                .execute(
                    "DELETE FROM client_sessions_v1 WHERE account_id=?1",
                    [account_id],
                )
                .map_err(storage_error)?;
            Ok(())
        })
        .await
    }

    // 保留过期行，验证认证入口确实检查持久化期限。
    #[cfg(test)]
    pub(crate) async fn expire_client_session(
        &self,
        token_hash: String,
    ) -> Result<bool, AccountError> {
        self.blocking(move |connection| {
            let now = chrono::Utc::now().timestamp_millis();
            let changed = connection
                .execute(
                    "UPDATE client_sessions_v1 SET issued_at=?1,expires_at=?2 WHERE token_hash=?3",
                    params![now - 1, now, token_hash],
                )
                .map_err(storage_error)?;
            Ok(changed == 1)
        })
        .await
    }
}

fn purge_inactive(connection: &Connection, now: i64) -> Result<usize, AccountError> {
    connection
        .execute(
            "DELETE FROM client_sessions_v1 WHERE expires_at<=?1 OR last_seen_at<=?2 OR NOT EXISTS (
                SELECT 1 FROM accounts a WHERE a.id=client_sessions_v1.account_id
                AND a.enabled=1 AND a.auth_version=client_sessions_v1.auth_version
            )",
            params![now, now - SESSION_IDLE_TIMEOUT_MS],
        )
        .map_err(storage_error)
}
