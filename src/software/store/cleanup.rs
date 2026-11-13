use super::{
    Inner, PackageMetadata, SoftwareStore, StoreError, positive_number, reject_links, storage,
    valid_id,
};
use rusqlite::{OptionalExtension, Transaction, TransactionBehavior, params};
use serde::Serialize;
use std::{
    collections::HashSet,
    fs,
    io::ErrorKind,
    path::{Path, PathBuf},
};

const PLATFORM_PRODUCT: &str = "mskdsp-platform";

// 增量表与旧版软件仓库兼容，旧程序仍可读取包及通道。
// 显式 UPSERT 避免外层通道 UPSERT 覆盖触发器的 OR IGNORE 冲突策略。
pub(super) const SCHEMA: &str = "
    BEGIN IMMEDIATE;
    CREATE TABLE IF NOT EXISTS package_cleanup_settings(
        id INTEGER PRIMARY KEY CHECK(id=1),
        days INTEGER NOT NULL CHECK(days IN (7,30,45,60,90))
    );
    CREATE TABLE IF NOT EXISTS package_publications(
        package_id TEXT NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
        channel TEXT NOT NULL,
        PRIMARY KEY(package_id,channel)
    );
    DROP TRIGGER IF EXISTS remember_package_publication_insert;
    DROP TRIGGER IF EXISTS remember_package_publication_update;
    DROP TRIGGER IF EXISTS record_package_publication_insert;
    DROP TRIGGER IF EXISTS record_package_publication_update;
    DELETE FROM package_publications WHERE channel='unknown';
    INSERT INTO package_publications(package_id,channel)
    SELECT package_id,channel FROM channels WHERE true
    ON CONFLICT(package_id,channel) DO NOTHING;
    CREATE TRIGGER IF NOT EXISTS track_package_publication_insert
    AFTER INSERT ON channels BEGIN
        INSERT INTO package_publications VALUES(NEW.package_id,NEW.channel)
        ON CONFLICT(package_id,channel) DO NOTHING;
    END;
    CREATE TRIGGER IF NOT EXISTS track_package_publication_update
    AFTER UPDATE OF package_id ON channels BEGIN
        INSERT INTO package_publications VALUES(NEW.package_id,NEW.channel)
        ON CONFLICT(package_id,channel) DO NOTHING;
    END;
    CREATE TABLE IF NOT EXISTS pending_package_deletions(
        id TEXT PRIMARY KEY,
        product TEXT NOT NULL,
        version TEXT NOT NULL,
        platform TEXT NOT NULL,
        metadata TEXT NOT NULL,
        UNIQUE(product,version,platform)
    );
    COMMIT;";

#[derive(Serialize)]
pub(crate) struct CleanupPackage {
    pub id: String,
    product: String,
    version: String,
    platform: String,
    size: u64,
    created_at: String,
}

#[derive(Serialize)]
pub(crate) struct CleanupPreview {
    pub packages: Vec<CleanupPackage>,
    total_bytes: u64,
}

#[derive(Default, Serialize)]
pub(crate) struct CleanupResult {
    pub deleted: usize,
    pub freed_bytes: u64,
    pub pending: usize,
}

struct Candidate {
    id: String,
    metadata: PackageMetadata,
    created_at: String,
}

fn validate_cleanup_days(days: u16) -> Result<(), StoreError> {
    if matches!(days, 7 | 30 | 45 | 60 | 90) {
        Ok(())
    } else {
        Err(StoreError::Invalid("请选择有效的 CI 安装包保留期限".into()))
    }
}

impl SoftwareStore {
    pub(crate) fn with_runtime_directory(mut self, directory: Option<PathBuf>) -> Self {
        self.runtime_directory = directory;
        self
    }

    pub(crate) async fn cleanup_settings(&self) -> Result<Option<u16>, StoreError> {
        self.run(|inner| inner.cleanup_days()).await
    }

    pub(crate) async fn set_cleanup_days(
        &self,
        days: Option<u16>,
    ) -> Result<Option<Result<CleanupResult, StoreError>>, StoreError> {
        if let Some(days) = days {
            validate_cleanup_days(days)?;
        }
        let runtime = self.runtime_directory.clone();
        self.run(move |inner| {
            if let Some(days) = days {
                inner
                    .db
                    .execute(
                        "INSERT INTO package_cleanup_settings VALUES(1,?1)
                         ON CONFLICT(id) DO UPDATE SET days=excluded.days",
                        [days],
                    )
                    .map_err(storage)?;
                Ok(Some(inner.clean_packages(days, None, runtime.as_deref())))
            } else {
                inner
                    .db
                    .execute("DELETE FROM package_cleanup_settings WHERE id=1", [])
                    .map_err(storage)?;
                Ok(None)
            }
        })
        .await
    }

    pub(crate) async fn preview_cleanup(&self, days: u16) -> Result<CleanupPreview, StoreError> {
        validate_cleanup_days(days)?;
        let runtime = self.runtime_directory.clone();
        self.run(move |inner| {
            let updater_lock = lock_updater(runtime.as_deref())?;
            let runtime = runtime.as_deref().filter(|_| updater_lock.is_some());
            let candidates = inner.cleanup_candidates(days, runtime)?;
            let mut total_bytes = 0;
            let packages = candidates
                .into_iter()
                .map(|candidate| {
                    let m = candidate.metadata;
                    total_bytes += m.size;
                    CleanupPackage {
                        id: candidate.id,
                        product: m.product,
                        version: m.version,
                        platform: m.platform,
                        size: m.size,
                        created_at: candidate.created_at,
                    }
                })
                .collect();
            Ok(CleanupPreview {
                packages,
                total_bytes,
            })
        })
        .await
    }

    pub(crate) async fn cleanup_once(
        &self,
        days: u16,
        ids: Vec<String>,
    ) -> Result<CleanupResult, StoreError> {
        validate_cleanup_days(days)?;
        let mut selected = HashSet::with_capacity(ids.len());
        for id in ids {
            valid_id(&id)?;
            if !selected.insert(id) {
                return Err(StoreError::Invalid("清理请求包含重复的软件包".into()));
            }
        }
        if selected.is_empty() {
            return Err(StoreError::Invalid("请先预览待清理的软件包".into()));
        }
        let runtime = self.runtime_directory.clone();
        self.run(move |inner| inner.clean_packages(days, Some(&selected), runtime.as_deref()))
            .await
    }

    pub(crate) async fn cleanup_scheduled(&self) -> Result<Option<CleanupResult>, StoreError> {
        let runtime = self.runtime_directory.clone();
        self.run(move |inner| {
            if let Some(days) = inner.cleanup_days()? {
                return inner
                    .clean_packages(days, None, runtime.as_deref())
                    .map(Some);
            }
            // 即使关闭自动清理，也要完成此前已批准并提交的软件包删除。
            let mut result = CleanupResult::default();
            inner.finish_pending_deletions(&mut result)?;
            Ok((result.freed_bytes > 0 || result.pending > 0).then_some(result))
        })
        .await
    }
}

impl Inner {
    fn cleanup_days(&self) -> Result<Option<u16>, StoreError> {
        self.db
            .query_row(
                "SELECT days FROM package_cleanup_settings WHERE id=1",
                [],
                |row| row.get(0),
            )
            .optional()
            .map_err(storage)
    }

    fn cleanup_candidates(
        &self,
        days: u16,
        runtime: Option<&Path>,
    ) -> Result<Vec<Candidate>, StoreError> {
        let cutoff = chrono::Utc::now() - chrono::Duration::days(i64::from(days));
        let cutoff_text = cutoff.to_rfc3339_opts(chrono::SecondsFormat::Secs, true);
        let protected = runtime.and_then(|path| platform_versions(path, &self.db));
        let mut statement = self
            .db
            .prepare(
                "SELECT p.id,p.metadata,p.created,p.product,p.version,p.platform
                 FROM packages p
                 WHERE p.created<?1
                   AND NOT EXISTS(SELECT 1 FROM channels c WHERE c.package_id=p.id)
                   AND NOT EXISTS(SELECT 1 FROM package_publications h
                                  WHERE h.package_id=p.id AND h.channel<>'ci')
                 ORDER BY p.created,p.id",
            )
            .map_err(storage)?;
        let mut rows = statement.query([cutoff_text]).map_err(storage)?;
        let mut candidates = Vec::new();
        while let Some(row) = rows.next().map_err(storage)? {
            let id: String = row.get(0).map_err(storage)?;
            let raw: String = row.get(1).map_err(storage)?;
            let created_at: String = row.get(2).map_err(storage)?;
            let product: String = row.get(3).map_err(storage)?;
            let version: String = row.get(4).map_err(storage)?;
            let platform: String = row.get(5).map_err(storage)?;
            let m: PackageMetadata = serde_json::from_str(&raw).map_err(storage)?;
            if valid_id(&id).is_err()
                || m.product != product
                || m.version != version
                || m.platform != platform
                || !chrono::DateTime::parse_from_rfc3339(&created_at)
                    .is_ok_and(|created| created.to_utc() < cutoff)
                || !ci_identity(&m)
                || (product == PLATFORM_PRODUCT
                    && !protected
                        .as_ref()
                        .is_some_and(|keep| !keep.contains(&version)))
            {
                continue;
            }
            let Ok(path) = self.package_path(&m) else {
                continue;
            };
            if !fs::metadata(path).is_ok_and(|info| info.is_file() && info.len() == m.size) {
                continue;
            }
            candidates.push(Candidate {
                id,
                metadata: m,
                created_at,
            });
        }
        Ok(candidates)
    }

    fn clean_packages(
        &mut self,
        days: u16,
        selected: Option<&HashSet<String>>,
        runtime: Option<&Path>,
    ) -> Result<CleanupResult, StoreError> {
        let updater_lock = lock_updater(runtime)?;
        let runtime = runtime.filter(|_| updater_lock.is_some());
        let tx = Transaction::new_unchecked(&self.db, TransactionBehavior::Immediate)
            .map_err(storage)?;
        let mut candidates = self.cleanup_candidates(days, runtime)?;
        if let Some(selected) = selected {
            candidates.retain(|candidate| selected.contains(&candidate.id));
            if candidates.len() != selected.len() {
                return Err(StoreError::Conflict("清理预览已变化，请重新预览".into()));
            }
        }
        for candidate in &candidates {
            let m = &candidate.metadata;
            tx.execute(
                "INSERT INTO pending_package_deletions VALUES(?1,?2,?3,?4,?5)",
                params![
                    candidate.id,
                    m.product,
                    m.version,
                    m.platform,
                    serde_json::to_string(m).map_err(storage)?
                ],
            )
            .map_err(storage)?;
            tx.execute("DELETE FROM uploads WHERE package_id=?1", [&candidate.id])
                .map_err(storage)?;
            tx.execute("DELETE FROM packages WHERE id=?1", [&candidate.id])
                .map_err(storage)?;
        }
        tx.commit().map_err(storage)?;
        let mut result = CleanupResult {
            deleted: candidates.len(),
            ..Default::default()
        };
        self.finish_pending_deletions(&mut result)?;
        Ok(result)
    }

    fn finish_pending_deletions(&self, result: &mut CleanupResult) -> Result<(), StoreError> {
        // 待删除记录在包从目录移除的事务中提交。意外退出后可重试，并阻止同一身份重新上传。
        let tx = Transaction::new_unchecked(&self.db, TransactionBehavior::Immediate)
            .map_err(storage)?;
        let queued = {
            let mut query = tx
                .prepare("SELECT id,metadata FROM pending_package_deletions")
                .map_err(storage)?;
            query
                .query_map([], |row| {
                    Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
                })
                .map_err(storage)?
                .collect::<Result<Vec<_>, _>>()
                .map_err(storage)?
        };
        for (id, raw) in queued {
            let m: PackageMetadata = serde_json::from_str(&raw).map_err(storage)?;
            // 旧版程序回退后可能已重新接收该版本，此时文件归新上传任务所有。
            let reused: bool = tx
                .query_row(
                    "SELECT EXISTS(SELECT 1 FROM uploads WHERE product=?1 AND version=?2
                                   AND platform=?3 AND state IN ('uploading','completed'))
                         OR EXISTS(SELECT 1 FROM packages WHERE product=?1 AND version=?2
                                   AND platform=?3)",
                    params![m.product, m.version, m.platform],
                    |row| row.get(0),
                )
                .map_err(storage)?;
            let removal = if reused {
                Ok(0)
            } else {
                self.remove_queued_file(&m)
            };
            match removal {
                Ok(bytes) => {
                    tx.execute("DELETE FROM pending_package_deletions WHERE id=?1", [&id])
                        .map_err(storage)?;
                    result.freed_bytes += bytes;
                }
                Err(error) => {
                    tracing::warn!(package_id = %id, error = %error, "CI 安装包文件清理失败，将重试");
                    result.pending += 1;
                }
            }
        }
        tx.commit().map_err(storage)
    }

    fn remove_queued_file(&self, m: &PackageMetadata) -> Result<u64, StoreError> {
        let path = self.package_path(m)?;
        let size = match fs::metadata(&path) {
            Ok(info) if info.is_file() && info.len() == m.size => {
                fs::remove_file(&path).map_err(storage)?;
                info.len()
            }
            Err(error) if error.kind() == ErrorKind::NotFound => 0,
            Ok(_) => return Err(StoreError::Conflict("待清理文件与入库记录不一致".into())),
            Err(error) => return Err(storage(error)),
        };
        #[cfg(unix)]
        if let Some(parent) = path.parent() {
            match fs::File::open(parent) {
                Ok(directory) => directory.sync_all().map_err(storage)?,
                Err(error) if error.kind() == ErrorKind::NotFound => {}
                Err(error) => return Err(storage(error)),
            }
        }
        Ok(size)
    }
}

fn ci_identity(m: &PackageMetadata) -> bool {
    if m.channel != "ci" {
        return false;
    }
    match m.product.as_str() {
        PLATFORM_PRODUCT => super::platform_ci_identity(m).is_ok(),
        super::super::TESTBENCH_PRODUCT => super::validate_new_testbench_version(m).is_ok(),
        super::super::TESTBENCH_CASES_PRODUCT => super::validate_testbench_cases(m).is_ok(),
        "mskdsp-upper" | "mskdsp-lower" => device_ci_identity(m),
        _ => false,
    }
}

fn device_ci_identity(m: &PackageMetadata) -> bool {
    let Ok(version) = semver::Version::parse(&m.version) else {
        return false;
    };
    if m.source_sha.len() != 40
        || !m
            .source_sha
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    {
        return false;
    }
    let maintenance = format!("release/{}.{}", version.major, version.minor);
    let sha = &m.source_sha[..7];
    if m.product == "mskdsp-upper" {
        let Some(stamp) = version.pre.as_str().strip_prefix("ci.") else {
            return false;
        };
        (m.source_ref == "main" || m.source_ref == maintenance)
            && stamp.len() == 15
            && chrono::NaiveDateTime::parse_from_str(stamp, "%Y%m%dt%H%M%S").is_ok()
            && version.build.as_str() == format!("sha.{sha}")
    } else {
        let mut parts = version.pre.as_str().split('-');
        let package_version = format!("{}-{}", m.version, m.platform);
        (matches!(m.source_ref.as_str(), "main" | "master") || m.source_ref == maintenance)
            && version.build.is_empty()
            && parts.next() == Some("ci")
            && ['p', 'b', 'j'].into_iter().all(|prefix| {
                parts
                    .next()
                    .and_then(|part| part.strip_prefix(prefix))
                    .is_some_and(positive_number)
            })
            && parts.next() == Some(sha)
            && parts.next().is_none()
            && m.package_version == package_version
            && m.file_name == format!("mskdsp-{package_version}")
    }
}

#[cfg(unix)]
fn lock_updater(runtime: Option<&Path>) -> Result<Option<fs::File>, StoreError> {
    use rustix::{fs::FlockOperation, io::Errno};

    let Some(runtime) = runtime else {
        return Ok(None);
    };
    let path = runtime.join(".update.lock");
    reject_links(&path)?;
    let lock = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(storage)?;
    match rustix::fs::flock(&lock, FlockOperation::NonBlockingLockExclusive) {
        Ok(()) => Ok(Some(lock)),
        Err(Errno::WOULDBLOCK) => Ok(None),
        Err(error) => Err(storage(error)),
    }
}

#[cfg(not(unix))]
fn lock_updater(_: Option<&Path>) -> Result<Option<fs::File>, StoreError> {
    Ok(None)
}

fn platform_versions(runtime: &Path, db: &rusqlite::Connection) -> Option<HashSet<String>> {
    let path = runtime.join("state.json");
    reject_links(&path).ok()?;
    let state: serde_json::Value = serde_json::from_slice(&fs::read(path).ok()?).ok()?;
    let version = state.get("version")?.as_str()?.to_owned();
    if version.is_empty() {
        return None;
    }
    let mut protected = HashSet::from([version]);
    if let Some(previous) = state.get("previous") {
        let previous = previous.get("version")?.as_str()?.to_owned();
        if previous.is_empty() {
            return None;
        }
        protected.insert(previous);
    }
    // 平台更新器比较 SemVer 优先级，构建 SHA 不参与排序。并列最新均保留。
    let mut query = db
        .prepare(
            "SELECT version,metadata FROM packages WHERE product=?1 AND platform='linux-amd64'",
        )
        .ok()?;
    let mut rows = query.query([PLATFORM_PRODUCT]).ok()?;
    let mut latest: Vec<semver::Version> = Vec::new();
    while let Some(row) = rows.next().ok()? {
        let indexed_version: String = row.get(0).ok()?;
        let raw: String = row.get(1).ok()?;
        let metadata: PackageMetadata = serde_json::from_str(&raw).ok()?;
        if metadata.channel != "ci" {
            continue;
        }
        if metadata.product != PLATFORM_PRODUCT
            || metadata.platform != "linux-amd64"
            || metadata.version != indexed_version
            || !ci_identity(&metadata)
        {
            return None;
        }
        let version = semver::Version::parse(&metadata.version).ok()?;
        match latest.first().map(|head| version.cmp_precedence(head)) {
            None | Some(std::cmp::Ordering::Greater) => latest = vec![version],
            Some(std::cmp::Ordering::Equal) => latest.push(version),
            Some(std::cmp::Ordering::Less) => {}
        }
    }
    protected.extend(latest.into_iter().map(|version| version.to_string()));
    Some(protected)
}
