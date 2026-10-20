//! 设备身份及账号授权持久化；高熵设备令牌仅保存 SHA256 摘要。
use rusqlite::{Connection, OptionalExtension, params};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    path::PathBuf,
    sync::{Arc, Mutex},
    time::Duration,
};
use uuid::Uuid;

#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    #[error("{0}")]
    Invalid(String),
    #[error("设备不存在")]
    NotFound,
    #[error("设备标识已存在")]
    Conflict,
    #[error("设备凭据或访问授权无效")]
    Denied,
    #[error("设备存储暂不可用")]
    Storage,
}
#[derive(Clone, Debug, Serialize)]
pub struct DeviceRecord {
    pub device_id: String,
    pub display_name: String,
    pub enabled: bool,
    pub created_at: i64,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NewDevice {
    pub device_id: String,
    pub display_name: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DeviceUpdate {
    pub display_name: String,
    pub enabled: bool,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Grant {
    pub account_id: String,
    pub allow_grpc: bool,
    pub allow_ssh: bool,
}
#[derive(Serialize)]
pub struct CreatedDevice {
    #[serde(flatten)]
    pub device: DeviceRecord,
    pub token: String,
}
#[derive(Clone)]
pub struct DeviceStore(Arc<Mutex<Connection>>);
fn storage(error: impl std::fmt::Display) -> StoreError {
    tracing::error!(error=%error,"设备存储操作失败");
    StoreError::Storage
}
pub fn valid_service(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|v| v.is_ascii_alphanumeric() || matches!(v, b'_' | b'-' | b'.'))
}
fn name(value: &str) -> Result<String, StoreError> {
    let value = value.trim();
    if value.is_empty() || value.len() > 128 || value.chars().any(char::is_control) {
        return Err(StoreError::Invalid(
            "设备名称必须为 1～128 字节且无控制字符".into(),
        ));
    }
    Ok(value.to_owned())
}
fn digest(value: &str) -> String {
    format!("{:x}", Sha256::digest(value.as_bytes()))
}
fn token() -> String {
    format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple())
}
fn row(r: &rusqlite::Row<'_>) -> rusqlite::Result<DeviceRecord> {
    Ok(DeviceRecord {
        device_id: r.get(0)?,
        display_name: r.get(1)?,
        enabled: r.get(2)?,
        created_at: r.get(3)?,
    })
}
fn get(db: &Connection, id: &str) -> Result<DeviceRecord, StoreError> {
    db.query_row(
        "SELECT id,display_name,enabled,created_at FROM devices WHERE id=?1",
        [id],
        row,
    )
    .optional()
    .map_err(storage)?
    .ok_or(StoreError::NotFound)
}
impl DeviceStore {
    pub async fn open(path: PathBuf) -> Result<Self, StoreError> {
        tokio::task::spawn_blocking(move||{
        match std::fs::symlink_metadata(&path) {Ok(meta) if meta.is_file()&&!meta.file_type().is_symlink()=>{},Ok(_)=>return Err(StoreError::Storage),Err(e) if e.kind()==std::io::ErrorKind::NotFound=>{},Err(e)=>return Err(storage(e))}
        if let Some(parent)=path.parent(){std::fs::create_dir_all(parent).map_err(storage)?;}
        let db=Connection::open(path).map_err(storage)?;db.busy_timeout(Duration::from_secs(5)).map_err(storage)?;
        let check:String=db.query_row("PRAGMA quick_check",[],|r|r.get(0)).map_err(storage)?;if check!="ok"{return Err(StoreError::Storage)}
        let version:i64=db.pragma_query_value(None,"user_version",|r|r.get(0)).map_err(storage)?;
        if version==0 {
            let count:i64=db.query_row("SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'",[],|r|r.get(0)).map_err(storage)?;
            if count!=0{return Err(StoreError::Storage)}
            db.execute_batch("BEGIN IMMEDIATE; CREATE TABLE devices(id TEXT PRIMARY KEY NOT NULL,display_name TEXT NOT NULL,enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),token_hash TEXT NOT NULL UNIQUE,created_at INTEGER NOT NULL); CREATE TABLE grants(device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,account_id TEXT NOT NULL,allow_grpc INTEGER NOT NULL CHECK(allow_grpc IN (0,1)),allow_ssh INTEGER NOT NULL CHECK(allow_ssh IN (0,1)),PRIMARY KEY(device_id,account_id)); PRAGMA user_version=1; COMMIT;").map_err(storage)?;
        }else if version!=1{return Err(StoreError::Storage)}
        db.prepare("SELECT id,display_name,enabled,token_hash,created_at FROM devices LIMIT 0").map_err(storage)?;
        db.prepare("SELECT device_id,account_id,allow_grpc,allow_ssh FROM grants LIMIT 0").map_err(storage)?;
        db.pragma_update(None,"foreign_keys","ON").map_err(storage)?;db.pragma_update(None,"journal_mode","WAL").map_err(storage)?;
        Ok(Self(Arc::new(Mutex::new(db))))
    }).await.map_err(storage)?
    }
    async fn run<T: Send + 'static>(
        &self,
        op: impl FnOnce(&mut Connection) -> Result<T, StoreError> + Send + 'static,
    ) -> Result<T, StoreError> {
        let db = self.0.clone();
        tokio::task::spawn_blocking(move || {
            let mut db = db.lock().map_err(storage)?;
            op(&mut db)
        })
        .await
        .map_err(storage)?
    }
    pub async fn create(&self, input: NewDevice) -> Result<CreatedDevice, StoreError> {
        if !valid_service(&input.device_id) {
            return Err(StoreError::Invalid(
                "设备标识只允许 1～128 位字母、数字、点、横线或下划线".into(),
            ));
        }
        let display = name(&input.display_name)?;
        let token = token();
        let hashed = digest(&token);
        self.run(move|db|{let tx=db.transaction().map_err(storage)?;
            let count:i64=tx.query_row("SELECT count(*) FROM devices",[],|r|r.get(0)).map_err(storage)?;if count>=10_000{return Err(StoreError::Invalid("设备数量已达到上限".into()))}
            if get(&tx,&input.device_id).is_ok(){return Err(StoreError::Conflict)}
            tx.execute("INSERT INTO devices(id,display_name,enabled,token_hash,created_at)VALUES(?1,?2,1,?3,?4)",params![input.device_id,display,hashed,chrono::Utc::now().timestamp_millis()]).map_err(storage)?;
            let device=get(&tx,&input.device_id)?;tx.commit().map_err(storage)?;tracing::info!(device_id=%device.device_id,"已创建设备身份");Ok(CreatedDevice{device,token})}).await
    }
    pub async fn get(&self, id: &str) -> Result<DeviceRecord, StoreError> {
        let id = id.to_owned();
        self.run(move |db| get(db, &id)).await
    }
    pub async fn list(&self) -> Result<Vec<DeviceRecord>, StoreError> {
        self.run(|db|{let mut q=db.prepare("SELECT id,display_name,enabled,created_at FROM devices ORDER BY created_at DESC,id").map_err(storage)?;q.query_map([],row).map_err(storage)?.collect::<Result<Vec<_>,_>>().map_err(storage)}).await
    }
    pub async fn update(&self, id: &str, input: DeviceUpdate) -> Result<DeviceRecord, StoreError> {
        let id = id.to_owned();
        let display = name(&input.display_name)?;
        self.run(move |db| {
            get(db, &id)?;
            db.execute(
                "UPDATE devices SET display_name=?1,enabled=?2 WHERE id=?3",
                params![display, input.enabled, id],
            )
            .map_err(storage)?;
            get(db, &id)
        })
        .await
    }
    pub async fn rotate(&self, id: &str) -> Result<CreatedDevice, StoreError> {
        let id = id.to_owned();
        let token = token();
        let hashed = digest(&token);
        self.run(move |db| {
            let device = get(db, &id)?;
            db.execute(
                "UPDATE devices SET token_hash=?1 WHERE id=?2",
                params![hashed, id],
            )
            .map_err(storage)?;
            Ok(CreatedDevice { device, token })
        })
        .await
    }
    pub async fn delete(&self, id: &str) -> Result<(), StoreError> {
        let id = id.to_owned();
        self.run(move |db| {
            if db
                .execute("DELETE FROM devices WHERE id=?1", [id])
                .map_err(storage)?
                == 0
            {
                return Err(StoreError::NotFound);
            }
            Ok(())
        })
        .await
    }
    pub async fn authenticate(&self, token: &str) -> Result<DeviceRecord, StoreError> {
        if token.len() != 64 || !token.bytes().all(|c| c.is_ascii_hexdigit()) {
            return Err(StoreError::Denied);
        }
        let hashed = digest(token);
        self.run(move|db|db.query_row("SELECT id,display_name,enabled,created_at FROM devices WHERE token_hash=?1 AND enabled=1",[hashed],row).optional().map_err(storage)?.ok_or(StoreError::Denied)).await
    }
    pub async fn grants(&self, id: &str) -> Result<Vec<Grant>, StoreError> {
        let id = id.to_owned();
        self.run(move|db|{get(db,&id)?;let mut q=db.prepare("SELECT account_id,allow_grpc,allow_ssh FROM grants WHERE device_id=?1 ORDER BY account_id").map_err(storage)?;q.query_map([id],|r|Ok(Grant{account_id:r.get(0)?,allow_grpc:r.get(1)?,allow_ssh:r.get(2)?})).map_err(storage)?.collect::<Result<Vec<_>,_>>().map_err(storage)}).await
    }
    pub async fn set_grants(&self, id: &str, grants: Vec<Grant>) -> Result<(), StoreError> {
        let mut ids = HashSet::new();
        if grants.len() > 1024
            || grants.iter().any(|g| {
                g.account_id.is_empty()
                    || g.account_id.len() > 128
                    || !ids.insert(g.account_id.clone())
            })
        {
            return Err(StoreError::Invalid(
                "授权账号无效、重复或超过 1024 个".into(),
            ));
        }
        let id = id.to_owned();
        self.run(move|db|{let tx=db.transaction().map_err(storage)?;get(&tx,&id)?;tx.execute("DELETE FROM grants WHERE device_id=?1",[&id]).map_err(storage)?;for grant in grants{tx.execute("INSERT INTO grants(device_id,account_id,allow_grpc,allow_ssh)VALUES(?1,?2,?3,?4)",params![id,grant.account_id,grant.allow_grpc,grant.allow_ssh]).map_err(storage)?;}tx.commit().map_err(storage)?;Ok(())}).await
    }
    pub async fn for_account(
        &self,
        account: &str,
    ) -> Result<Vec<(DeviceRecord, Grant)>, StoreError> {
        let account = account.to_owned();
        self.run(move|db|{let mut q=db.prepare("SELECT d.id,d.display_name,d.enabled,d.created_at,g.allow_grpc,g.allow_ssh FROM devices d JOIN grants g ON d.id=g.device_id WHERE g.account_id=?1 AND d.enabled=1 AND (g.allow_grpc=1 OR g.allow_ssh=1) ORDER BY d.display_name,d.id").map_err(storage)?;q.query_map([&account],|r|Ok((row(r)?,Grant{account_id:account.clone(),allow_grpc:r.get(4)?,allow_ssh:r.get(5)?}))).map_err(storage)?.collect::<Result<Vec<_>,_>>().map_err(storage)}).await
    }
    pub async fn authorize(
        &self,
        id: &str,
        account: &str,
        service: &str,
    ) -> Result<(), StoreError> {
        let id = id.to_owned();
        let account = account.to_owned();
        let ssh = service == "ssh";
        self.run(move|db|{let allowed:bool=db.query_row("SELECT EXISTS(SELECT 1 FROM devices d JOIN grants g ON d.id=g.device_id WHERE d.id=?1 AND d.enabled=1 AND g.account_id=?2 AND ((?3=1 AND g.allow_ssh=1) OR (?3=0 AND g.allow_grpc=1)))",params![id,account,ssh],|r|r.get(0)).map_err(storage)?;if allowed{Ok(())}else{Err(StoreError::Denied)}}).await
    }
}
