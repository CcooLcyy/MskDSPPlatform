use rusqlite::{Connection, OptionalExtension, Transaction, params};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    path::PathBuf,
    sync::{Arc, Mutex},
    time::Duration,
};
use subtle::ConstantTimeEq;
use uuid::Uuid;

#[derive(Debug, thiserror::Error)]
pub enum IdentityError {
    #[error("{0}")]
    Invalid(String),
    #[error("上位机身份不存在")]
    NotFound,
    #[error("身份已被修改，请刷新后重试")]
    Conflict,
    #[error("上位机身份认证失败或接入权限已撤销")]
    Unauthorized,
    #[error("身份存储暂不可用，请稍后重试")]
    Storage,
}

fn storage(error: impl std::fmt::Display) -> IdentityError {
    tracing::error!(error = %error, "上位机身份数据库操作失败");
    IdentityError::Storage
}

#[derive(Clone)]
pub struct IdentityStore(Arc<Mutex<Connection>>);

#[derive(Debug, Clone, Serialize)]
pub struct AgentRecord {
    pub id: String,
    pub name: String,
    pub site_id: String,
    pub notes: String,
    pub enabled: bool,
    pub credential_active: bool,
    pub credential_version: i64,
    pub revision: i64,
    pub created_at: i64,
    pub updated_at: i64,
    pub last_seen_at: Option<i64>,
    pub agent_version: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NewAgent {
    pub name: String,
    #[serde(default)]
    pub site_id: String,
    #[serde(default)]
    pub notes: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AgentUpdate {
    pub name: String,
    pub site_id: String,
    pub notes: String,
    pub enabled: bool,
}

#[derive(Debug, Serialize)]
pub struct IssuedCredential {
    pub agent: AgentRecord,
    pub token: String,
}

#[derive(Debug)]
pub struct AuthenticatedAgent {
    pub id: String,
    pub site_id: String,
    pub credential_version: i64,
}

#[derive(Debug, Serialize)]
pub struct IdentityEvent {
    pub id: i64,
    pub agent_id: String,
    pub actor: String,
    pub action: String,
    pub created_at: i64,
}

const COLUMNS: &str = "id,name,site_id,notes,enabled,token_digest IS NOT NULL,credential_version,revision,created_at,updated_at,last_seen_at,agent_version";

fn record(row: &rusqlite::Row<'_>) -> rusqlite::Result<AgentRecord> {
    Ok(AgentRecord {
        id: row.get(0)?,
        name: row.get(1)?,
        site_id: row.get(2)?,
        notes: row.get(3)?,
        enabled: row.get(4)?,
        credential_active: row.get(5)?,
        credential_version: row.get(6)?,
        revision: row.get(7)?,
        created_at: row.get(8)?,
        updated_at: row.get(9)?,
        last_seen_at: row.get(10)?,
        agent_version: row.get(11)?,
    })
}

fn get(db: &Connection, id: &str) -> Result<AgentRecord, IdentityError> {
    db.query_row(
        &format!("SELECT {COLUMNS} FROM agents WHERE id=?1"),
        [id],
        record,
    )
    .optional()
    .map_err(storage)?
    .ok_or(IdentityError::NotFound)
}

fn current(db: &Connection, id: &str, revision: i64) -> Result<AgentRecord, IdentityError> {
    let existing = get(db, id)?;
    if revision < 1 || existing.revision != revision {
        return Err(IdentityError::Conflict);
    }
    Ok(existing)
}

fn event(tx: &Transaction<'_>, id: &str, actor: &str, action: &str) -> Result<(), IdentityError> {
    tx.execute(
        "INSERT INTO identity_events(agent_id,actor,action,created_at) VALUES (?1,?2,?3,?4)",
        params![id, actor, action, now()],
    )
    .map_err(storage)?;
    Ok(())
}

fn now() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

fn token() -> String {
    format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple())
}

fn digest(token: &str) -> Vec<u8> {
    Sha256::digest(token.as_bytes()).to_vec()
}

fn fields(
    name: &str,
    site_id: &str,
    notes: &str,
) -> Result<(String, String, String), IdentityError> {
    let name = name.trim();
    let site_id = site_id.trim();
    if name.is_empty() || name.len() > 128 || name.chars().any(char::is_control) {
        return Err(IdentityError::Invalid(
            "名称必须为 1～128 字节且不能含控制字符".into(),
        ));
    }
    if site_id.len() > 128 || site_id.chars().any(char::is_control) {
        return Err(IdentityError::Invalid(
            "现场编号最多 128 字节且不能含控制字符".into(),
        ));
    }
    if notes.len() > 2048
        || notes
            .chars()
            .any(|c| c.is_control() && !matches!(c, '\n' | '\r' | '\t'))
    {
        return Err(IdentityError::Invalid(
            "备注最多 2048 字节且不能含非法控制字符".into(),
        ));
    }
    Ok((name.to_owned(), site_id.to_owned(), notes.to_owned()))
}

impl IdentityStore {
    pub async fn open(path: PathBuf) -> Result<Self, IdentityError> {
        tokio::task::spawn_blocking(move || {
            let parent = path.parent().ok_or_else(|| IdentityError::Invalid("身份数据库路径不正确".into()))?;
            std::fs::create_dir_all(parent).map_err(storage)?;
            match std::fs::symlink_metadata(&path) {
                Ok(meta) if !meta.file_type().is_file() => return Err(storage("身份数据库必须为普通文件")),
                Err(error) if error.kind() != std::io::ErrorKind::NotFound => return Err(storage(error)),
                _ => {}
            }
            let mut db = Connection::open(path).map_err(storage)?;
            db.busy_timeout(Duration::from_secs(5)).map_err(storage)?;
            db.pragma_update(None, "foreign_keys", true).map_err(storage)?;
            db.pragma_update(None, "synchronous", "FULL").map_err(storage)?;
            let tx = db.transaction().map_err(storage)?;
            let version: i64 = tx.pragma_query_value(None, "user_version", |row| row.get(0)).map_err(storage)?;
            if version == 0 {
                let tables: i64 = tx.query_row("SELECT COUNT(*) FROM sqlite_master WHERE type='table'", [], |row| row.get(0)).map_err(storage)?;
                if tables != 0 { return Err(storage("未标识版本的身份数据库不为空")); }
                tx.execute_batch("CREATE TABLE agents (
                    id TEXT PRIMARY KEY NOT NULL,name TEXT NOT NULL,site_id TEXT NOT NULL,notes TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),token_digest BLOB,
                    credential_version INTEGER NOT NULL,revision INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,last_seen_at INTEGER,agent_version TEXT NOT NULL,
                    CHECK(token_digest IS NULL OR length(token_digest)=32)
                );
                CREATE TABLE identity_events (
                    id INTEGER PRIMARY KEY,agent_id TEXT NOT NULL REFERENCES agents(id),
                    actor TEXT NOT NULL,action TEXT NOT NULL,created_at INTEGER NOT NULL
                );
                CREATE INDEX identity_events_agent ON identity_events(agent_id,id DESC);
                PRAGMA user_version=1;").map_err(storage)?;
            } else if version != 1 { return Err(storage("身份数据库版本不受支持")); }
            tx.prepare(&format!("SELECT {COLUMNS},token_digest FROM agents LIMIT 0")).map_err(storage)?;
            tx.prepare("SELECT id,agent_id,actor,action,created_at FROM identity_events LIMIT 0").map_err(storage)?;
            tx.commit().map_err(storage)?;
            tracing::info!("上位机身份存储已初始化");
            Ok(Self(Arc::new(Mutex::new(db))))
        }).await.map_err(storage)?
    }

    async fn run<T: Send + 'static>(
        &self,
        operation: impl FnOnce(&mut Connection) -> Result<T, IdentityError> + Send + 'static,
    ) -> Result<T, IdentityError> {
        let db = self.0.clone();
        tokio::task::spawn_blocking(move || {
            let mut database = db.lock().map_err(storage)?;
            operation(&mut database)
        })
        .await
        .map_err(storage)?
    }

    pub async fn list(&self) -> Result<Vec<AgentRecord>, IdentityError> {
        self.run(|db| {
            db.prepare(&format!(
                "SELECT {COLUMNS} FROM agents ORDER BY updated_at DESC,id"
            ))
            .map_err(storage)?
            .query_map([], record)
            .map_err(storage)?
            .collect::<rusqlite::Result<Vec<_>>>()
            .map_err(storage)
        })
        .await
    }

    pub async fn get(&self, id: &str) -> Result<AgentRecord, IdentityError> {
        let id = id.to_owned();
        self.run(move |db| get(db, &id)).await
    }

    pub async fn register(
        &self,
        input: NewAgent,
        actor: &str,
    ) -> Result<IssuedCredential, IdentityError> {
        let (name, site_id, notes) = fields(&input.name, &input.site_id, &input.notes)?;
        let actor = actor.to_owned();
        self.run(move |db| {
            let tx = db.transaction().map_err(storage)?;
            let count: i64 = tx
                .query_row("SELECT COUNT(*) FROM agents", [], |r| r.get(0))
                .map_err(storage)?;
            if count >= 10000 {
                return Err(IdentityError::Invalid(
                    "上位机身份数量已达到 10000 个上限".into(),
                ));
            }
            let id = Uuid::new_v4().to_string();
            let token = token();
            tx.execute(
                "INSERT INTO agents VALUES (?1,?2,?3,?4,1,?5,1,1,?6,?6,NULL,'')",
                params![id, name, site_id, notes, digest(&token), now()],
            )
            .map_err(storage)?;
            event(&tx, &id, &actor, "registered")?;
            let agent = get(&tx, &id)?;
            tx.commit().map_err(storage)?;
            tracing::info!(agent_id=%id, actor=%actor, "上位机身份已登记");
            Ok(IssuedCredential { agent, token })
        })
        .await
    }

    pub async fn update(
        &self,
        id: &str,
        revision: i64,
        input: AgentUpdate,
        actor: &str,
    ) -> Result<AgentRecord, IdentityError> {
        let (name, site_id, notes) = fields(&input.name, &input.site_id, &input.notes)?;
        let (id, actor) = (id.to_owned(), actor.to_owned());
        self.run(move |db| {
            let tx = db.transaction().map_err(storage)?;
            let previous = current(&tx,&id,revision)?;
            let invalidate = previous.site_id != site_id || previous.enabled != input.enabled;
            tx.execute("UPDATE agents SET name=?1,site_id=?2,notes=?3,enabled=?4,credential_version=credential_version+?5,revision=revision+1,updated_at=?6 WHERE id=?7",params![name,site_id,notes,input.enabled,i64::from(invalidate),now(),id]).map_err(storage)?;
            let action = if previous.enabled != input.enabled { if input.enabled {"enabled"} else {"disabled"} } else {"updated"};
            event(&tx,&id,&actor,action)?;
            let agent = get(&tx,&id)?;
            tx.commit().map_err(storage)?;
            tracing::info!(agent_id=%id, actor=%actor, action, "上位机身份资料已更新");
            Ok(agent)
        }).await
    }

    pub async fn rotate(
        &self,
        id: &str,
        revision: i64,
        actor: &str,
    ) -> Result<IssuedCredential, IdentityError> {
        let (id, actor) = (id.to_owned(), actor.to_owned());
        self.run(move |db| {
            let tx = db.transaction().map_err(storage)?;
            current(&tx,&id,revision)?;
            let token = token();
            tx.execute("UPDATE agents SET token_digest=?1,credential_version=credential_version+1,revision=revision+1,updated_at=?2 WHERE id=?3",params![digest(&token),now(),id]).map_err(storage)?;
            event(&tx,&id,&actor,"credential_rotated")?;
            let agent = get(&tx,&id)?;
            tx.commit().map_err(storage)?;
            tracing::info!(agent_id=%id, actor=%actor, "上位机接入凭据已轮换");
            Ok(IssuedCredential { agent, token })
        }).await
    }

    pub async fn revoke(
        &self,
        id: &str,
        revision: i64,
        actor: &str,
    ) -> Result<AgentRecord, IdentityError> {
        self.revoke_or_unbind(id, revision, actor, false).await
    }

    pub async fn unbind(
        &self,
        id: &str,
        revision: i64,
        actor: &str,
    ) -> Result<AgentRecord, IdentityError> {
        self.revoke_or_unbind(id, revision, actor, true).await
    }

    async fn revoke_or_unbind(
        &self,
        id: &str,
        revision: i64,
        actor: &str,
        unbind: bool,
    ) -> Result<AgentRecord, IdentityError> {
        let (id, actor) = (id.to_owned(), actor.to_owned());
        self.run(move |db| {
            let tx = db.transaction().map_err(storage)?;
            current(&tx,&id,revision)?;
            tx.execute("UPDATE agents SET token_digest=NULL,site_id=CASE WHEN ?1 THEN '' ELSE site_id END,enabled=CASE WHEN ?1 THEN 0 ELSE enabled END,credential_version=credential_version+1,revision=revision+1,updated_at=?2 WHERE id=?3",params![unbind,now(),id]).map_err(storage)?;
            let action = if unbind {"unbound"} else {"credential_revoked"};
            event(&tx,&id,&actor,action)?;
            let agent = get(&tx,&id)?;
            tx.commit().map_err(storage)?;
            tracing::info!(agent_id=%id, actor=%actor, action, "上位机接入权限已撤销");
            Ok(agent)
        }).await
    }

    pub async fn events(&self, id: &str) -> Result<Vec<IdentityEvent>, IdentityError> {
        let id = id.to_owned();
        self.run(move |db| {
            get(db,&id)?;
            db.prepare("SELECT id,agent_id,actor,action,created_at FROM identity_events WHERE agent_id=?1 ORDER BY id DESC LIMIT 100").map_err(storage)?
                .query_map([id], |row| Ok(IdentityEvent {id:row.get(0)?,agent_id:row.get(1)?,actor:row.get(2)?,action:row.get(3)?,created_at:row.get(4)?})).map_err(storage)?
                .collect::<rusqlite::Result<Vec<_>>>().map_err(storage)
        }).await
    }

    pub async fn authenticate(
        &self,
        id: &str,
        token: &str,
        site_id: &str,
        agent_version: &str,
    ) -> Result<AuthenticatedAgent, IdentityError> {
        if token.len() != 64
            || !token.bytes().all(|c| c.is_ascii_hexdigit())
            || agent_version.len() > 128
            || agent_version.chars().any(char::is_control)
        {
            return Err(IdentityError::Unauthorized);
        }
        let (id, site_id, hash) = (id.to_owned(), site_id.to_owned(), digest(token));
        self.run(move |db| {
            let tx = db.transaction().map_err(storage)?;
            let identity = match get(&tx, &id) {
                Err(IdentityError::NotFound) => return Err(IdentityError::Unauthorized),
                other => other?,
            };
            let stored: Option<Vec<u8>> = tx
                .query_row(
                    "SELECT token_digest FROM agents WHERE id=?1",
                    [&id],
                    |row| row.get(0),
                )
                .map_err(storage)?;
            if !identity.enabled
                || identity.site_id != site_id
                || !stored.is_some_and(|stored| bool::from(stored.ct_eq(&hash)))
            {
                return Err(IdentityError::Unauthorized);
            }
            tx.commit().map_err(storage)?;
            Ok(AuthenticatedAgent {
                id,
                site_id: identity.site_id,
                credential_version: identity.credential_version,
            })
        })
        .await
    }

    pub async fn check_session(
        &self,
        id: &str,
        credential_version: i64,
    ) -> Result<(), IdentityError> {
        self.session(id, credential_version, false).await
    }

    pub async fn connected(
        &self,
        id: &str,
        credential_version: i64,
        agent_version: &str,
    ) -> Result<(), IdentityError> {
        if agent_version.len() > 128 || agent_version.chars().any(char::is_control) {
            return Err(IdentityError::Unauthorized);
        }
        let (id, agent_version) = (id.to_owned(), agent_version.to_owned());
        self.run(move |db| {
            let count = db.execute(
                "UPDATE agents SET last_seen_at=?1,agent_version=?2 WHERE id=?3 AND credential_version=?4 AND enabled=1 AND token_digest IS NOT NULL",
                params![now(),agent_version,id,credential_version],
            ).map_err(storage)?;
            if count != 1 { return Err(IdentityError::Unauthorized); }
            Ok(())
        }).await
    }

    pub async fn heartbeat(&self, id: &str, credential_version: i64) -> Result<(), IdentityError> {
        self.session(id, credential_version, true).await
    }

    async fn session(
        &self,
        id: &str,
        credential_version: i64,
        heartbeat: bool,
    ) -> Result<(), IdentityError> {
        let id = id.to_owned();
        self.run(move |db| {
            let identity = match get(db, &id) {
                Err(IdentityError::NotFound) => return Err(IdentityError::Unauthorized),
                other => other?,
            };
            if !identity.enabled
                || !identity.credential_active
                || identity.credential_version != credential_version
            {
                return Err(IdentityError::Unauthorized);
            }
            if heartbeat {
                db.execute(
                    "UPDATE agents SET last_seen_at=?1 WHERE id=?2",
                    params![now(), id],
                )
                .map_err(storage)?;
            }
            Ok(())
        })
        .await
    }
}

#[cfg(test)]
#[path = "identity_tests.rs"]
mod tests;
