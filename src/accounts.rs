//! 平台维护的上位机登录账号；数据库只持久化加盐的密码摘要。
use argon2::{Argon2, PasswordHash, PasswordVerifier};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use serde::{Deserialize, Serialize};
use std::{
    path::PathBuf,
    sync::{Arc, Mutex},
    time::Duration,
};
use tracing::{error, info};
use uuid::Uuid;

const MAX_ACCOUNTS: i64 = 10_000;
const SCHEMA_VERSION: i64 = 2;
// 未知账号也执行相同类型的密码验证，避免通过快速失败枚举账号。
const DUMMY_HASH: &str = "$argon2id$v=19$m=19456,t=2,p=1$zXP+U5DsQuiMPtr8V8OuAQ$qhq/Ws7G6jkYExvvfYr/k4qqo1+/TOlpBo6DcKRl5ME";
const COLUMNS: &str = "id,username,display_name,enabled,revision,auth_version,created_at,updated_at,last_login_at,role";

#[derive(Debug, thiserror::Error)]
pub enum AccountError {
    #[error("{0}")]
    Invalid(String),
    #[error("账号不存在")]
    NotFound,
    #[error("账号已被其他操作更新，请刷新后重试")]
    Conflict,
    #[error("用户名已存在")]
    DuplicateUsername,
    #[error("用户名或密码不正确，或账号已停用")]
    Unauthorized,
    #[error("账号存储暂时不可用")]
    Storage,
}

#[derive(Clone, Debug, Serialize)]
pub struct AccountRecord {
    pub id: String,
    pub username: String,
    pub display_name: String,
    pub role: String,
    pub enabled: bool,
    pub revision: i64,
    #[serde(skip_serializing)]
    pub auth_version: i64,
    pub created_at: i64,
    pub updated_at: i64,
    pub last_login_at: Option<i64>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NewAccount {
    pub username: String,
    pub password: String,
    #[serde(default)]
    pub display_name: String,
    #[serde(default = "default_role")]
    pub role: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AccountUpdate {
    pub display_name: String,
    pub enabled: bool,
    #[serde(default)]
    pub role: Option<String>,
}

#[derive(Clone)]
pub struct AccountStore(Arc<Mutex<Connection>>);

impl AccountStore {
    /// 传入完整数据库路径，例如 data/accounts/accounts.sqlite3。
    pub async fn open(path: PathBuf) -> Result<Self, AccountError> {
        tokio::task::spawn_blocking(move || {
            match std::fs::symlink_metadata(&path) {
                Ok(metadata) if metadata.file_type().is_file() => {}
                Ok(_) => {
                    error!("账号数据库路径必须是普通文件");
                    return Err(AccountError::Storage);
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(error) => return Err(storage_error(error)),
            }
            if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
                std::fs::create_dir_all(parent).map_err(storage_error)?;
            }
            let mut connection = Connection::open(path).map_err(storage_error)?;
            connection
                .busy_timeout(Duration::from_secs(5))
                .map_err(storage_error)?;
            let check: String = connection
                .query_row("PRAGMA quick_check", [], |r| r.get(0))
                .map_err(storage_error)?;
            if check != "ok" {
                error!("账号数据库完整性检查失败");
                return Err(AccountError::Storage);
            }
            let version: i64 = connection
                .pragma_query_value(None, "user_version", |r| r.get(0))
                .map_err(storage_error)?;
            match version {
                0 => {
                    let objects: i64 = connection
                        .query_row(
                            "SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'",
                            [],
                            |r| r.get(0),
                        )
                        .map_err(storage_error)?;
                    if objects != 0 {
                        error!("账号数据库缺少有效版本信息");
                        return Err(AccountError::Storage);
                    }
                    connection
                        .execute_batch(
                            "BEGIN IMMEDIATE;
                         CREATE TABLE accounts (
                           id TEXT PRIMARY KEY NOT NULL,
                           username TEXT NOT NULL UNIQUE,
                           display_name TEXT NOT NULL,
                           password_hash TEXT NOT NULL,
                           enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                           revision INTEGER NOT NULL CHECK(revision > 0),
                           auth_version INTEGER NOT NULL CHECK(auth_version > 0),
                           created_at INTEGER NOT NULL,
                           updated_at INTEGER NOT NULL,
                           last_login_at INTEGER,
                           role TEXT NOT NULL DEFAULT 'operator' CHECK(role IN ('admin','operator'))
                         );
                         PRAGMA user_version=2;
                         COMMIT;",
                        )
                        .map_err(storage_error)?;
                }
                1 => {
                    let transaction = connection
                        .transaction_with_behavior(TransactionBehavior::Immediate)
                        .map_err(storage_error)?;
                    transaction.prepare("SELECT id,username,display_name,enabled,revision,auth_version,created_at,updated_at,last_login_at,password_hash FROM accounts LIMIT 0")
                        .map_err(storage_error)?;
                    transaction.execute_batch(
                        "ALTER TABLE accounts ADD COLUMN role TEXT NOT NULL DEFAULT 'operator' CHECK(role IN ('admin','operator'));
                         PRAGMA user_version=2;",
                    ).map_err(storage_error)?;
                    // 验证完整旧结构后再提交，异常旧库不会留下部分迁移。
                    transaction.prepare(&format!("SELECT {COLUMNS},password_hash FROM accounts LIMIT 0"))
                        .map_err(storage_error)?;
                    transaction.commit().map_err(storage_error)?;
                    info!("上位机账号数据库已迁移，已有账号角色设为运维人员");
                }
                SCHEMA_VERSION => {}
                _ => {
                    error!("账号数据库版本不受支持");
                    return Err(AccountError::Storage);
                }
            }
            // 已声明版本的文件也必须包含完整结构，不能作为新库悄悄覆盖。
            connection
                .prepare(&format!(
                    "SELECT {COLUMNS},password_hash FROM accounts LIMIT 0"
                ))
                .map_err(storage_error)?;
            let count: i64 = connection
                .query_row("SELECT count(*) FROM accounts", [], |r| r.get(0))
                .map_err(storage_error)?;
            if count > MAX_ACCOUNTS {
                error!("账号数据库记录数量超出限制");
                return Err(AccountError::Storage);
            }
            connection
                .pragma_update(None, "journal_mode", "WAL")
                .map_err(storage_error)?;
            sessions::initialize(&connection)?;
            Ok(Self(Arc::new(Mutex::new(connection))))
        })
        .await
        .map_err(storage_error)?
    }

    async fn blocking<T, F>(&self, operation: F) -> Result<T, AccountError>
    where
        T: Send + 'static,
        F: FnOnce(&mut Connection) -> Result<T, AccountError> + Send + 'static,
    {
        let connection = self.0.clone();
        tokio::task::spawn_blocking(move || {
            let mut connection = connection.lock().map_err(storage_error)?;
            operation(&mut connection)
        })
        .await
        .map_err(storage_error)?
    }

    pub async fn create(&self, new: NewAccount) -> Result<AccountRecord, AccountError> {
        let username = normalize_username(&new.username)?;
        validate_password(&new.password)?;
        let display_name = normalize_display_name(&new.display_name, &username)?;
        validate_role(&new.role)?;
        self.blocking(move |connection| {
            let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate).map_err(storage_error)?;
            let duplicate: bool = transaction.query_row("SELECT EXISTS(SELECT 1 FROM accounts WHERE username=?1)", [&username], |r| r.get(0)).map_err(storage_error)?;
            if duplicate { return Err(AccountError::DuplicateUsername); }
            let count: i64 = transaction.query_row("SELECT count(*) FROM accounts", [], |r| r.get(0)).map_err(storage_error)?;
            if count >= MAX_ACCOUNTS { return Err(AccountError::Invalid("账号数量已达到上限".into())); }
            let password_hash = crate::auth::hash_password(&new.password).map_err(storage_error)?;
            let id = Uuid::new_v4().to_string();
            let now = chrono::Utc::now().timestamp_millis();
            transaction.execute("INSERT INTO accounts (id,username,display_name,password_hash,enabled,revision,auth_version,created_at,updated_at,role) VALUES (?1,?2,?3,?4,1,1,1,?5,?5,?6)", params![id, username, display_name, password_hash, now, new.role]).map_err(storage_error)?;
            let record = get_record(&transaction, &id)?;
            transaction.commit().map_err(storage_error)?;
            info!(account_id = %id, role = %record.role, "已创建上位机账号");
            Ok(record)
        }).await
    }

    pub async fn list(&self) -> Result<Vec<AccountRecord>, AccountError> {
        self.blocking(|connection| {
            let mut statement = connection
                .prepare(&format!(
                    "SELECT {COLUMNS} FROM accounts ORDER BY created_at DESC,id ASC"
                ))
                .map_err(storage_error)?;
            statement
                .query_map([], row_record)
                .map_err(storage_error)?
                .collect::<Result<Vec<_>, _>>()
                .map_err(storage_error)
        })
        .await
    }

    pub async fn get(&self, id: &str) -> Result<AccountRecord, AccountError> {
        let id = id.to_owned();
        self.blocking(move |connection| get_record(connection, &id))
            .await
    }

    pub async fn update(
        &self,
        id: &str,
        revision: i64,
        update: AccountUpdate,
    ) -> Result<AccountRecord, AccountError> {
        let id = id.to_owned();
        self.blocking(move |connection| {
            let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate).map_err(storage_error)?;
            let old = get_record(&transaction, &id)?;
            if old.revision != revision { return Err(AccountError::Conflict); }
            let display_name = normalize_display_name(&update.display_name, &old.username)?;
            let role = update.role.as_deref().unwrap_or(&old.role);
            validate_role(role)?;
            transaction.execute("UPDATE accounts SET display_name=?1,enabled=?2,revision=revision+1,auth_version=auth_version+?3,updated_at=?4,role=?7 WHERE id=?5 AND revision=?6", params![display_name, update.enabled, i64::from(old.enabled != update.enabled), chrono::Utc::now().timestamp_millis(), id, revision, role]).map_err(storage_error)?;
            let record = get_record(&transaction, &id)?;
            transaction.commit().map_err(storage_error)?;
            info!(account_id = %id, enabled = record.enabled, old_role = %old.role, role = %record.role, "已更新上位机账号及角色");
            Ok(record)
        }).await
    }

    pub async fn reset_password(
        &self,
        id: &str,
        revision: i64,
        password: String,
    ) -> Result<AccountRecord, AccountError> {
        validate_password(&password)?;
        let id = id.to_owned();
        self.blocking(move |connection| {
            let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate).map_err(storage_error)?;
            let old = get_record(&transaction, &id)?;
            if old.revision != revision { return Err(AccountError::Conflict); }
            let hash = crate::auth::hash_password(&password).map_err(storage_error)?;
            transaction.execute("UPDATE accounts SET password_hash=?1,revision=revision+1,auth_version=auth_version+1,updated_at=?2 WHERE id=?3 AND revision=?4", params![hash, chrono::Utc::now().timestamp_millis(), id, revision]).map_err(storage_error)?;
            let record = get_record(&transaction, &id)?;
            transaction.commit().map_err(storage_error)?;
            info!(account_id = %id, "已重置上位机账号密码");
            Ok(record)
        }).await
    }

    pub async fn verify(
        &self,
        username: &str,
        password: &str,
    ) -> Result<AccountRecord, AccountError> {
        let username = normalize_username(username).unwrap_or_default();
        let password = password.to_owned();
        self.blocking(move |connection| {
            let result = connection
                .query_row(
                    &format!("SELECT {COLUMNS},password_hash FROM accounts WHERE username=?1"),
                    [&username],
                    |row| Ok((row_record(row)?, row.get::<_, String>(10)?)),
                )
                .optional()
                .map_err(storage_error)?;
            let hash = result
                .as_ref()
                .map_or(DUMMY_HASH, |(_, hash)| hash.as_str());
            let parsed = PasswordHash::new(hash).map_err(|_| {
                error!("账号密码摘要格式异常");
                AccountError::Storage
            })?;
            let valid = Argon2::default()
                .verify_password(password.as_bytes(), &parsed)
                .is_ok();
            match result {
                Some((record, _)) if valid && record.enabled => Ok(record),
                _ => Err(AccountError::Unauthorized),
            }
        })
        .await
    }

    #[cfg(test)]
    pub async fn check_authorization(
        &self,
        id: &str,
        auth_version: i64,
    ) -> Result<AccountRecord, AccountError> {
        let id = id.to_owned();
        self.blocking(move |connection| {
            let record = match get_record(connection, &id) {
                Err(AccountError::NotFound) => return Err(AccountError::Unauthorized),
                result => result?,
            };
            if record.enabled && record.auth_version == auth_version {
                Ok(record)
            } else {
                Err(AccountError::Unauthorized)
            }
        })
        .await
    }

    #[cfg(test)]
    pub async fn touch_login(
        &self,
        id: &str,
        auth_version: i64,
    ) -> Result<AccountRecord, AccountError> {
        let id = id.to_owned();
        self.blocking(move |connection| {
            let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate).map_err(storage_error)?;
            let changed = transaction.execute("UPDATE accounts SET last_login_at=?1 WHERE id=?2 AND enabled=1 AND auth_version=?3", params![chrono::Utc::now().timestamp_millis(), id, auth_version]).map_err(storage_error)?;
            if changed != 1 { return Err(AccountError::Unauthorized); }
            let record = get_record(&transaction, &id)?;
            transaction.commit().map_err(storage_error)?;
            Ok(record)
        }).await
    }
}

fn normalize_username(value: &str) -> Result<String, AccountError> {
    let normalized = value.trim().to_ascii_lowercase();
    if !(3..=64).contains(&normalized.len())
        || !normalized
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_-.".contains(&byte))
    {
        return Err(AccountError::Invalid(
            "用户名必须为 3～64 字节，只能包含英文字母、数字、下划线、短横线或点".into(),
        ));
    }
    Ok(normalized)
}

fn default_role() -> String {
    "operator".into()
}

fn validate_role(role: &str) -> Result<(), AccountError> {
    if matches!(role, "admin" | "operator") {
        Ok(())
    } else {
        Err(AccountError::Invalid(
            "账号角色必须为管理员或运维人员".into(),
        ))
    }
}

fn normalize_display_name(value: &str, username: &str) -> Result<String, AccountError> {
    if value.chars().any(char::is_control) {
        return Err(AccountError::Invalid("显示名称不能包含控制字符".into()));
    }
    let value = value.trim();
    let value = if value.is_empty() { username } else { value };
    if value.len() > 128 {
        return Err(AccountError::Invalid("显示名称必须为 1～128 字节".into()));
    }
    Ok(value.to_owned())
}

fn validate_password(password: &str) -> Result<(), AccountError> {
    if !(8..=128).contains(&password.len()) || password.trim().is_empty() {
        return Err(AccountError::Invalid(
            "密码必须为 8～128 字节，且不能全部为空白".into(),
        ));
    }
    Ok(())
}

fn row_record(row: &rusqlite::Row<'_>) -> rusqlite::Result<AccountRecord> {
    Ok(AccountRecord {
        id: row.get(0)?,
        username: row.get(1)?,
        display_name: row.get(2)?,
        enabled: row.get(3)?,
        revision: row.get(4)?,
        auth_version: row.get(5)?,
        created_at: row.get(6)?,
        updated_at: row.get(7)?,
        last_login_at: row.get(8)?,
        role: row.get(9)?,
    })
}

fn get_record(connection: &Connection, id: &str) -> Result<AccountRecord, AccountError> {
    connection
        .query_row(
            &format!("SELECT {COLUMNS} FROM accounts WHERE id=?1"),
            [id],
            row_record,
        )
        .optional()
        .map_err(storage_error)?
        .ok_or(AccountError::NotFound)
}

fn storage_error(_error: impl std::fmt::Display) -> AccountError {
    // 数据库与任务错误可能包含 SQL、路径或意外数据，公开错误和日志均不展开。
    error!("上位机账号存储操作失败");
    AccountError::Storage
}

#[cfg(test)]
#[path = "account_store_tests.rs"]
mod tests;

#[path = "account_sessions.rs"]
pub(crate) mod sessions;
