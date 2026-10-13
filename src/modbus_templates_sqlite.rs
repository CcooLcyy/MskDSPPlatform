use super::*;
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use std::time::Duration;

const MARKER: &str = "database-required.json";
const PREVIOUS_MARKER: &str = "database-required-previous.json";
const MARKER_BYTES: &[u8] = b"{\"storage\":\"sqlite\",\"schema_version\":1}\n";

fn storage(error: impl std::fmt::Display) -> StoreError {
    StoreError::Storage(format!("点表数据库操作失败：{error}"))
}

// 固定宽度文本完整保存 u64，同时使更新时间索引的文本顺序与数值顺序一致。
fn number(value: u64) -> String {
    format!("{value:020}")
}

fn parse_number(value: String) -> Result<u64, StoreError> {
    value.parse().map_err(storage)
}

fn check_regular(path: &Path) -> Result<bool, StoreError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.file_type().is_file() => Ok(true),
        Ok(_) => Err(StoreError::Storage(
            "点表存储路径必须是普通文件，不能是目录或符号链接".into(),
        )),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(storage(error)),
    }
}

fn legacy_paths(root: &Path) -> Result<Vec<(String, PathBuf)>, StoreError> {
    let mut paths = Vec::new();
    for entry in fs::read_dir(root).map_err(storage)? {
        let path = entry.map_err(storage)?.path();
        if path.extension().and_then(|value| value.to_str()) != Some("json")
            || matches!(
                path.file_name().and_then(|value| value.to_str()),
                Some(MARKER | PREVIOUS_MARKER)
            )
        {
            continue;
        }
        let id = path
            .file_stem()
            .and_then(|value| value.to_str())
            .ok_or_else(|| storage("旧点表文件名不合法"))?
            .to_owned();
        valid_platform_id(&id).map_err(|_| storage("旧点表文件名不是标准 UUID"))?;
        paths.push((id, path));
    }
    Ok(paths)
}

pub(super) fn open_initialized(root: &Path) -> Result<Connection, StoreError> {
    let path = root.join("templates.sqlite3");
    let exists = check_regular(&path)?;
    if !exists
        && (root.join(MARKER).exists()
            || root.join(PREVIOUS_MARKER).exists()
            || root.join("legacy-json").exists())
    {
        return Err(storage(
            "数据库缺失但迁移标记或备份仍存在，请恢复数据库，不能重建空库",
        ));
    }
    let mut database = Connection::open(&path).map_err(storage)?;
    database
        .busy_timeout(Duration::from_secs(10))
        .map_err(storage)?;
    database
        .pragma_update(None, "synchronous", "FULL")
        .map_err(storage)?;
    let transaction = database
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(storage)?;
    let version: u32 = transaction
        .pragma_query_value(None, "user_version", |row| row.get(0))
        .map_err(storage)?;
    if version != 0 && version != 1 {
        return Err(storage("点表数据库版本不受支持"));
    }
    if version == 0 {
        transaction.execute_batch(
        "CREATE TABLE templates (
            id TEXT PRIMARY KEY NOT NULL,
            revision TEXT NOT NULL CHECK(length(revision)=20 AND revision <> '00000000000000000000'),
            created_at TEXT NOT NULL CHECK(length(created_at)=20),
            updated_at TEXT NOT NULL CHECK(length(updated_at)=20 AND updated_at>=created_at),
            name TEXT NOT NULL, manufacturer TEXT NOT NULL, models_json TEXT NOT NULL,
            protocol_version TEXT NOT NULL, address_base INTEGER NOT NULL CHECK(address_base IN (0,1)),
            point_count INTEGER NOT NULL CHECK(point_count BETWEEN 1 AND 10000), template_json TEXT NOT NULL
        );
        CREATE INDEX templates_updated ON templates(updated_at DESC, id ASC);
        CREATE TABLE storage_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE legacy_files (id TEXT PRIMARY KEY, original_bytes BLOB NOT NULL);
        PRAGMA user_version=1;"
    ).map_err(storage)?;
    } else {
        // 已迁移数据库只检查结构；缺表或缺列必须拒绝，不能自动补成空库。
        transaction.prepare("SELECT id,revision,created_at,updated_at,name,manufacturer,models_json,protocol_version,address_base,point_count,template_json FROM templates LIMIT 0").map_err(storage)?;
        transaction
            .prepare("SELECT key,value FROM storage_meta LIMIT 0")
            .map_err(storage)?;
        transaction
            .prepare("SELECT id,original_bytes FROM legacy_files LIMIT 0")
            .map_err(storage)?;
        let indexes: u32 = transaction.query_row("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='templates_updated' AND tbl_name='templates'", [], |row| row.get(0)).map_err(storage)?;
        if indexes != 1 {
            return Err(storage("点表数据库摘要索引缺失，请恢复数据库"));
        }
    }
    let state: Option<String> = transaction
        .query_row(
            "SELECT value FROM storage_meta WHERE key='legacy_migration'",
            [],
            |row| row.get(0),
        )
        .optional()
        .map_err(storage)?;
    if state.is_none() {
        if version != 0 {
            return Err(storage("已迁移数据库的迁移状态缺失，请恢复数据库"));
        }
        if root.join(MARKER).exists() {
            return Err(storage("数据库缺少迁移状态，不能覆盖迁移标记"));
        }
        let paths = legacy_paths(root)?;
        if paths.len() > MAX_TEMPLATES {
            return Err(storage("旧点表数量超过 1000 张上限"));
        }
        let existing: usize = transaction
            .query_row("SELECT COUNT(*) FROM templates", [], |row| row.get(0))
            .map_err(storage)?;
        if existing != 0 {
            return Err(storage("数据库已有记录但缺少迁移状态，拒绝混合导入"));
        }
        for (id, _) in &paths {
            // 入库记录和恢复用快照必须来自同一次受大小限制的读取。
            let (record, bytes) = read_record_with_bytes(root, id)?;
            insert(&transaction, &record)?;
            transaction
                .execute(
                    "INSERT INTO legacy_files(id, original_bytes) VALUES (?1,?2)",
                    params![id, bytes],
                )
                .map_err(storage)?;
        }
        transaction
            .execute(
                "INSERT INTO storage_meta(key,value) VALUES ('legacy_migration','pending')",
                [],
            )
            .map_err(storage)?;
        tracing::info!(
            count = paths.len(),
            "旧点表已完整导入数据库事务，准备清理原始 JSON"
        );
    } else if state.as_deref() == Some("complete") {
        prepare_previous_cleanup(root, &transaction)?;
    }
    transaction.commit().map_err(storage)?;

    // 数据库事务提交后仍保留待清理快照；清理失败时不提供服务，下一次初始化可重试。
    let transaction = database
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(storage)?;
    let state: String = transaction
        .query_row(
            "SELECT value FROM storage_meta WHERE key='legacy_migration'",
            [],
            |row| row.get(0),
        )
        .map_err(storage)?;
    match state.as_str() {
        "pending" => finish_cleanup(root, &transaction)?,
        "complete" => {
            if !marker_directory(root)? {
                return Err(storage("数据库兼容保护目录缺失，请恢复保护目录"));
            }
            if !legacy_paths(root)?.is_empty() {
                return Err(storage("迁移后根目录再次出现旧 JSON，拒绝混用两套存储"));
            }
        }
        _ => return Err(storage("数据库迁移状态不合法")),
    }
    transaction.commit().map_err(storage)?;
    Ok(database)
}

fn marker_directory(root: &Path) -> Result<bool, StoreError> {
    match fs::symlink_metadata(root.join(MARKER)) {
        Ok(metadata) if metadata.file_type().is_dir() => {
            if fs::read_dir(root.join(MARKER))
                .map_err(storage)?
                .next()
                .is_some()
            {
                return Err(storage(
                    "数据库兼容保护目录必须为空，拒绝删除其中的其他文件",
                ));
            }
            Ok(true)
        }
        Ok(metadata) if metadata.file_type().is_file() => {
            if !matches_snapshot(&root.join(MARKER), MARKER_BYTES)? {
                return Err(storage("旧数据库迁移标记内容不合法"));
            }
            Ok(false)
        }
        Ok(_) => Err(storage("数据库兼容保护目录不能是符号链接")),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(storage(error)),
    }
}

fn archive_paths(root: &Path) -> Result<Vec<(String, PathBuf)>, StoreError> {
    let archive = root.join("legacy-json");
    match fs::symlink_metadata(&archive) {
        Ok(metadata) if metadata.file_type().is_dir() => (),
        Ok(_) => return Err(storage("旧点表归档路径必须是普通目录")),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(storage(error)),
    }
    let mut paths = Vec::new();
    for entry in fs::read_dir(&archive).map_err(storage)? {
        let path = entry.map_err(storage)?.path();
        let id = path
            .file_stem()
            .and_then(|value| value.to_str())
            .ok_or_else(|| storage("归档文件名不合法"))?
            .to_owned();
        if path.extension().and_then(|value| value.to_str()) != Some("json")
            || valid_platform_id(&id).is_err()
        {
            return Err(storage("旧点表归档包含非点表文件，拒绝自动清理"));
        }
        check_regular(&path)?;
        paths.push((id, path));
    }
    Ok(paths)
}

fn prepare_previous_cleanup(root: &Path, database: &Connection) -> Result<(), StoreError> {
    let directory = marker_directory(root)?;
    if !directory && !root.join(MARKER).exists() && !root.join(PREVIOUS_MARKER).exists() {
        return Err(storage("数据库兼容保护目录缺失，请恢复保护目录"));
    }
    if !legacy_paths(root)?.is_empty() {
        return Err(storage("迁移完成后出现新的旧 JSON，拒绝删除未导入的点表"));
    }
    let paths = archive_paths(root)?;
    if !directory || root.join("legacy-json").exists() || root.join(PREVIOUS_MARKER).exists() {
        for (id, _) in paths {
            // 旧归档可能对应已更新或已删除的点表，仅验证原记录，不覆盖当前数据库。
            let (_, bytes) = read_record_with_bytes(&root.join("legacy-json"), &id)?;
            database
                .execute(
                    "INSERT INTO legacy_files(id,original_bytes) VALUES (?1,?2)",
                    params![id, bytes],
                )
                .map_err(storage)?;
        }
        database
            .execute(
                "UPDATE storage_meta SET value='pending' WHERE key='legacy_migration'",
                [],
            )
            .map_err(storage)?;
    }
    Ok(())
}

fn ensure_marker_directory(root: &Path) -> Result<(), StoreError> {
    let marker = root.join(MARKER);
    let previous = root.join(PREVIOUS_MARKER);
    let directory = marker_directory(root)?;
    let previous_exists = check_regular(&previous)?;
    if previous_exists && !matches_snapshot(&previous, MARKER_BYTES)? {
        return Err(storage("旧迁移标记转换恢复文件损坏，拒绝自动删除"));
    }
    if !directory {
        if marker.exists() {
            // 转换期间始终至少保留一个非 UUID 的 .json 路径，让旧版持续拒绝创建空库。
            if previous_exists {
                fs::remove_file(&marker).map_err(storage)?;
            } else {
                fs::rename(&marker, &previous).map_err(storage)?;
            }
            sync_directory(root)?;
        }
        fs::create_dir(&marker).map_err(storage)?;
        sync_directory(root)?;
    }
    if previous.exists() {
        fs::remove_file(previous).map_err(storage)?;
    }
    sync_directory(root)
}

fn matches_snapshot(path: &Path, bytes: &[u8]) -> Result<bool, StoreError> {
    if fs::metadata(path).map_err(storage)?.len() != bytes.len() as u64 {
        return Ok(false);
    }
    let mut actual = Vec::new();
    File::open(path)
        .map_err(storage)?
        .take((bytes.len() + 1) as u64)
        .read_to_end(&mut actual)
        .map_err(storage)?;
    Ok(actual == bytes)
}

fn finish_cleanup(root: &Path, database: &Connection) -> Result<(), StoreError> {
    ensure_marker_directory(root)?;
    let files = database
        .prepare("SELECT id FROM legacy_files ORDER BY id")
        .map_err(storage)?
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(storage)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(storage)?;
    let archive = root.join("legacy-json");
    let expected: HashSet<_> = files.iter().map(String::as_str).collect();
    for (id, _) in legacy_paths(root)?.into_iter().chain(archive_paths(root)?) {
        if !expected.contains(id.as_str()) {
            return Err(storage("迁移事务提交后新增旧 JSON，拒绝丢弃未导入记录"));
        }
    }
    for id in &files {
        // 每次只加载一张原始点表，避免总量合法的迁移占用数 GiB 内存。
        let bytes: Vec<u8> = database
            .query_row(
                "SELECT original_bytes FROM legacy_files WHERE id=?1",
                [id],
                |row| row.get(0),
            )
            .map_err(storage)?;
        let source = root.join(format!("{id}.json"));
        let backup = archive.join(format!("{id}.json"));
        let source_exists = check_regular(&source)?;
        let backup_exists = check_regular(&backup)?;
        if source_exists && !matches_snapshot(&source, &bytes)? {
            return Err(storage("迁移事务提交后原始 JSON 已变化，拒绝覆盖或删除"));
        }
        if backup_exists && !matches_snapshot(&backup, &bytes)? {
            return Err(storage("旧 JSON 备份与迁移快照不同，拒绝覆盖备份"));
        }
        // 两处文件都已不存在，说明上次清理中断；已提交的数据库快照确保可继续。
        if source_exists {
            fs::remove_file(&source).map_err(storage)?;
        }
        if backup_exists {
            fs::remove_file(&backup).map_err(storage)?;
        }
    }
    if archive.exists() {
        sync_directory(&archive)?;
        fs::remove_dir(&archive).map_err(storage)?;
    }
    sync_directory(root)?;
    database
        .execute(
            "UPDATE storage_meta SET value='complete' WHERE key='legacy_migration'",
            [],
        )
        .map_err(storage)?;
    database
        .execute("DELETE FROM legacy_files", [])
        .map_err(storage)?;
    tracing::info!(
        count = files.len(),
        "点表数据库迁移清理完成，旧 JSON 文件已删除并禁止旧版本存储访问"
    );
    Ok(())
}

fn insert(database: &Connection, record: &Value) -> Result<(), StoreError> {
    let template = &record["template"];
    database.execute(
        "INSERT INTO templates(id,revision,created_at,updated_at,name,manufacturer,models_json,protocol_version,address_base,point_count,template_json)
         VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11)",
        params![record["id"].as_str().unwrap(), number(record["revision"].as_u64().unwrap()),
            number(record["created_at"].as_u64().unwrap()), number(record["updated_at"].as_u64().unwrap()),
            template["name"].as_str().unwrap(), template["manufacturer"].as_str().unwrap(),
            serde_json::to_string(&template["models"]).map_err(storage)?, template["protocol_version"].as_str().unwrap(),
            template["address_base"].as_f64().unwrap() as u64, template["points"].as_array().unwrap().len() as u64,
            serde_json::to_string(template).map_err(storage)?]
    ).map_err(storage)?;
    Ok(())
}

pub(super) fn list(database: &mut Connection) -> Result<Vec<Value>, StoreError> {
    let mut statement = database.prepare("SELECT id,revision,created_at,updated_at,name,manufacturer,models_json,protocol_version,address_base,point_count FROM templates ORDER BY updated_at DESC,id ASC").map_err(storage)?;
    let rows = statement
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, String>(4)?,
                row.get::<_, String>(5)?,
                row.get::<_, String>(6)?,
                row.get::<_, String>(7)?,
                row.get::<_, u64>(8)?,
                row.get::<_, u64>(9)?,
            ))
        })
        .map_err(storage)?;
    rows.map(|row| {
        let (id, revision, created, updated, name, manufacturer, models, protocol, base, count) = row.map_err(storage)?;
        let models: Value = serde_json::from_str(&models).map_err(storage)?;
        Ok(json!({"id":id,"revision":parse_number(revision)?,"created_at":parse_number(created)?,"updated_at":parse_number(updated)?,
            "name":name,"manufacturer":manufacturer,"models":models,"protocol_version":protocol,"address_base":base,"point_count":count}))
    }).collect()
}

pub(super) fn get(database: &Connection, id: &str) -> Result<Value, StoreError> {
    let row = database
        .query_row(
            "SELECT revision,created_at,updated_at,template_json FROM templates WHERE id=?1",
            [id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                ))
            },
        )
        .optional()
        .map_err(storage)?
        .ok_or(StoreError::NotFound)?;
    if row.3.len() > MAX_TEMPLATE_BYTES {
        return Err(storage("数据库点表内容超过大小上限"));
    }
    let template: Value = serde_json::from_str(&row.3).map_err(storage)?;
    validate_template(&template)
        .map_err(|error| storage(format!("数据库点表内容损坏：{error}")))?;
    Ok(
        json!({"id":id,"revision":parse_number(row.0)?,"created_at":parse_number(row.1)?,"updated_at":parse_number(row.2)?,"template":template}),
    )
}

pub(super) fn create(database: &mut Connection, template: Value) -> Result<Value, StoreError> {
    let transaction = database
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(storage)?;
    let count: usize = transaction
        .query_row("SELECT COUNT(*) FROM templates", [], |row| row.get(0))
        .map_err(storage)?;
    if count >= MAX_TEMPLATES {
        return Err(StoreError::Capacity);
    }
    let id = Uuid::new_v4().to_string();
    let timestamp = now_millis()?;
    let record = json!({"id":id,"revision":1,"created_at":timestamp,"updated_at":timestamp,"template":template});
    insert(&transaction, &record)?;
    transaction.commit().map_err(storage)?;
    tracing::info!(id, "数据库点表已创建");
    Ok(record)
}

pub(super) fn update(
    database: &mut Connection,
    id: &str,
    revision: u64,
    template: Value,
) -> Result<Value, StoreError> {
    let transaction = database
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(storage)?;
    let old = get(&transaction, id)?;
    if old["revision"].as_u64() != Some(revision) {
        return Err(StoreError::Conflict);
    }
    let next = revision
        .checked_add(1)
        .ok_or_else(|| storage("点表版本号已达到上限"))?;
    let timestamp = now_millis()?.max(old["updated_at"].as_u64().unwrap());
    let record = json!({"id":id,"revision":next,"created_at":old["created_at"],"updated_at":timestamp,"template":template});
    transaction
        .execute(
            "DELETE FROM templates WHERE id=?1 AND revision=?2",
            params![id, number(revision)],
        )
        .map_err(storage)?;
    insert(&transaction, &record)?;
    transaction.commit().map_err(storage)?;
    tracing::info!(id, revision = next, "数据库点表已更新");
    Ok(record)
}

pub(super) fn delete(database: &mut Connection, id: &str, revision: u64) -> Result<(), StoreError> {
    let transaction = database
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(storage)?;
    let current: String = transaction
        .query_row("SELECT revision FROM templates WHERE id=?1", [id], |row| {
            row.get(0)
        })
        .optional()
        .map_err(storage)?
        .ok_or(StoreError::NotFound)?;
    if parse_number(current)? != revision {
        return Err(StoreError::Conflict);
    }
    transaction
        .execute(
            "DELETE FROM templates WHERE id=?1 AND revision=?2",
            params![id, number(revision)],
        )
        .map_err(storage)?;
    transaction.commit().map_err(storage)?;
    tracing::info!(id, "数据库点表已删除");
    Ok(())
}
