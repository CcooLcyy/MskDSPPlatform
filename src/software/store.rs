// 核心失败场景测试先于存储实现编写。
#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(windows)]
    #[test]
    fn canonical_windows_drive_prefix_is_accepted() {
        let dir = tempfile::tempdir().unwrap();
        let canonical = fs::canonicalize(dir.path()).unwrap();
        reject_links(&canonical.join("not-created-yet")).unwrap();
        SoftwareStore::new(canonical, 64).unwrap();
    }
    fn metadata(bytes: &[u8]) -> PackageMetadata {
        PackageMetadata {
            product: "mskdsp-lower".into(),
            version: "1.0.0".into(),
            platform: "linux-arm64".into(),
            file_name: "lower.tar.gz".into(),
            size: bytes.len() as u64,
            sha256: format!("{:x}", Sha256::digest(bytes)),
            package_version: "1.0.0".into(),
            image_id: format!("sha256:{}", "a".repeat(64)),
            ..Default::default()
        }
    }
    fn platform_metadata(bytes: &[u8]) -> PackageMetadata {
        PackageMetadata {
            product: "mskdsp-platform".into(),
            platform: "linux-amd64".into(),
            version: "0.0.1-ci.20261018t094236.6.98+sha.aaaaaaa".into(),
            file_name: "platform.binary.tar.gz".into(),
            source_ref: "main".into(),
            source_sha: "a".repeat(40),
            build_time: "2026-10-18T09:42:36Z".into(),
            channel: "ci".into(),
            ..metadata(bytes)
        }
    }
    // 平台完成入库只产生私有候选，管理员也不能公开发布或撤回。
    #[tokio::test]
    async fn platform_ci_upload_is_resumable_private_and_unpublishable() {
        let dir = tempfile::tempdir().unwrap();
        let store = SoftwareStore::new(dir.path().into(), 1024).unwrap();
        let m = platform_metadata(b"abcdef");
        let task = store.create_upload(m.clone()).await.unwrap();
        assert_eq!(store.create_upload(m.clone()).await.unwrap().id, task.id);
        store
            .append_chunk(&task.id, 0, b"abc".to_vec())
            .await
            .unwrap();
        drop(store);
        let store = SoftwareStore::new(dir.path().into(), 1024).unwrap();
        assert_eq!(store.get_upload(&task.id).await.unwrap().received_bytes, 3);
        store
            .append_chunk(&task.id, 3, b"def".to_vec())
            .await
            .unwrap();
        let package = store.complete_upload(&task.id).await.unwrap();
        assert_eq!(
            store.complete_upload(&task.id).await.unwrap().id,
            package.id
        );
        assert_eq!(package.metadata, m);
        assert!(!package.ever_published);
        assert!(package.channels.is_empty());
        assert!(
            dir.path()
                .join("packages")
                .join(&m.product)
                .join(&m.version)
                .join(&m.platform)
                .join(&m.file_name)
                .is_file()
        );
        assert!(store.publish(&package.id, "ci").await.is_err());
        assert!(store.withdraw(&package.id, "ci").await.is_err());
        assert!(
            store
                .download_file(&package.id, &m.file_name)
                .await
                .is_err()
        );
        assert!(
            store
                .manifest("mskdsp-platform", "ci", None, "https://example.test")
                .await
                .is_err()
        );
        let mut corrupt = m.clone();
        corrupt.version = "0.0.1-ci.20261018t094236.6.99+sha.aaaaaaa".into();
        let corrupt = store.create_upload(corrupt).await.unwrap();
        store
            .append_chunk(&corrupt.id, 0, b"xxxxxx".to_vec())
            .await
            .unwrap();
        assert!(store.complete_upload(&corrupt.id).await.is_err());
        assert_eq!(store.get_upload(&corrupt.id).await.unwrap().state, "failed");
        assert_eq!(store.list_packages().await.unwrap().len(), 1);
    }
    // 北京时间版本与历史 UTC 版本共存，跨年仍校验同一构建瞬间。
    #[test]
    fn platform_ci_identity_accepts_beijing_and_legacy_utc_versions() {
        let mut m = platform_metadata(b"abc");
        m.build_time = "2026-12-31T16:00:00Z".into();
        m.version = "0.0.1-ci.20270101t000000.6.98+sha.aaaaaaa".into();
        platform_ci_identity(&m).unwrap();
        m.version = "0.0.1-ci.20261231t160000.6.98+sha.aaaaaaa".into();
        platform_ci_identity(&m).unwrap();
        m.version = "0.0.1-ci.20270101t000001.6.98+sha.aaaaaaa".into();
        assert!(platform_ci_identity(&m).is_err());
    }
    // 拒绝非 CI/非 main、错时区或时间身份、短提交，以及超过平台独立限额的包。
    #[test]
    fn platform_ci_metadata_requires_complete_matching_build_identity() {
        let m = platform_metadata(b"abc");
        validate(&m, PACKAGE_LIMIT).unwrap();
        for field in [
            "platform",
            "channel",
            "source_ref",
            "source_sha",
            "build_time",
            "version",
            "file_name",
        ] {
            let mut bad = m.clone();
            match field {
                "platform" => bad.platform = "linux-arm64".into(),
                "channel" => bad.channel = "stable".into(),
                "source_ref" => bad.source_ref = "feature".into(),
                "source_sha" => bad.source_sha = "aaaaaaa".into(),
                "build_time" => bad.build_time = "2026-10-18T09:42:37Z".into(),
                "version" => bad.version = "0.0.1-ci.20261018t094236.6.98+sha.bbbbbbb".into(),
                _ => bad.file_name = "../platform.binary.tar.gz".into(),
            }
            assert!(
                validate(&bad, PACKAGE_LIMIT).is_err(),
                "错误字段应拒绝: {field}"
            );
        }
        let mut too_large = m.clone();
        too_large.size = 128 * 1024 * 1024 + 1;
        assert!(matches!(
            validate(&too_large, PACKAGE_LIMIT),
            Err(StoreError::Limit(_))
        ));
        let mut no_zone = m.clone();
        no_zone.build_time = "2026-10-18T09:42:36".into();
        assert!(validate(&no_zone, PACKAGE_LIMIT).is_err());
        let mut non_utc = m.clone();
        non_utc.build_time = "2026-10-18T17:42:36+08:00".into();
        assert!(validate(&non_utc, PACKAGE_LIMIT).is_err());
        let mut old = serde_json::to_value(metadata(b"old")).unwrap();
        old.as_object_mut().unwrap().remove("build_time");
        old.as_object_mut().unwrap().remove("channel");
        let old: PackageMetadata = serde_json::from_value(old).unwrap();
        assert!(old.build_time.is_empty() && old.channel.is_empty());
        validate(&old, PACKAGE_LIMIT).unwrap();
        let mut future = serde_json::to_value(&old).unwrap();
        assert!(future.get("build_time").is_none() && future.get("channel").is_none());
        future["future_metadata"] = serde_json::json!("兼容未来字段");
        assert_eq!(
            serde_json::from_value::<PackageMetadata>(future).unwrap(),
            old
        );
    }
    #[tokio::test]
    async fn rejects_bad_offsets_limits_paths_and_digest_without_publishing() {
        let dir = tempfile::tempdir().unwrap();
        let store = SoftwareStore::new(dir.path().into(), 32).unwrap();
        let mut bad = metadata(b"abc");
        bad.file_name = "../escape".into();
        assert!(matches!(
            store.create_upload(bad).await,
            Err(StoreError::Invalid(_))
        ));
        assert!(matches!(
            store.create_upload(metadata(&[0; 33])).await,
            Err(StoreError::Limit(_))
        ));
        let upload = store.create_upload(metadata(b"abc")).await.unwrap();
        assert!(matches!(
            store.append_chunk(&upload.id, 1, b"abc".to_vec()).await,
            Err(StoreError::Conflict(_))
        ));
        assert!(matches!(
            store
                .append_chunk(&upload.id, 0, vec![0; 1024 * 1024 + 1])
                .await,
            Err(StoreError::Limit(_))
        ));
        store
            .append_chunk(&upload.id, 0, b"bad".to_vec())
            .await
            .unwrap();
        assert!(store.complete_upload(&upload.id).await.is_err());
        assert_eq!(store.get_upload(&upload.id).await.unwrap().state, "failed");
        assert!(store.list_packages().await.unwrap().is_empty());
    }
    #[tokio::test]
    async fn restart_resume_publish_withdraw_and_immutable_identity() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().to_path_buf();
        let store = SoftwareStore::new(root.clone(), 64).unwrap();
        let upload = store.create_upload(metadata(b"abcdef")).await.unwrap();
        store
            .append_chunk(&upload.id, 0, b"abc".to_vec())
            .await
            .unwrap();
        drop(store);
        let store = SoftwareStore::new(root, 64).unwrap();
        assert_eq!(
            store.get_upload(&upload.id).await.unwrap().received_bytes,
            3
        );
        store
            .append_chunk(&upload.id, 3, b"def".to_vec())
            .await
            .unwrap();
        let package = store.complete_upload(&upload.id).await.unwrap();
        assert_eq!(
            store.complete_upload(&upload.id).await.unwrap().id,
            package.id
        );
        assert!(
            store
                .download_file(&package.id, "lower.tar.gz")
                .await
                .is_err()
        );
        assert!(matches!(
            store.create_upload(metadata(b"different")).await,
            Err(StoreError::Conflict(_))
        ));
        store.publish(&package.id, "stable").await.unwrap();
        let manifest = store
            .manifest(
                "mskdsp-lower",
                "stable",
                Some("linux-arm64"),
                "https://example.test",
            )
            .await
            .unwrap();
        assert_eq!(manifest["asset"]["sha256"], metadata(b"abcdef").sha256);
        assert!(
            store
                .download_file(&package.id, "lower.tar.gz")
                .await
                .is_ok()
        );
        store.withdraw(&package.id, "stable").await.unwrap();
        assert!(
            store
                .manifest(
                    "mskdsp-lower",
                    "stable",
                    Some("linux-arm64"),
                    "https://example.test"
                )
                .await
                .is_err()
        );
        assert!(
            store
                .download_file(&package.id, "lower.tar.gz")
                .await
                .is_ok()
        );
    }
    async fn upload(store: &SoftwareStore, m: PackageMetadata, data: &[u8]) -> Package {
        let task = store.create_upload(m).await.unwrap();
        store
            .append_chunk(&task.id, 0, data.to_vec())
            .await
            .unwrap();
        store.complete_upload(&task.id).await.unwrap()
    }
    #[tokio::test]
    async fn beta_publication_stops_but_legacy_release_survives_restart_and_withdrawal() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().to_path_buf();
        let store = SoftwareStore::new(root.clone(), 64).unwrap();
        let old = upload(&store, metadata(b"old"), b"old").await;
        let mut next = metadata(b"new");
        next.version = "1.0.1".into();
        let next = upload(&store, next, b"new").await;
        store.publish(&next.id, "ci").await.unwrap();
        store.publish(&next.id, "stable").await.unwrap();
        // 模拟升级前已落盘的 Beta 发布，不通过待废止的发布入口造数据。
        {
            let inner = store.inner.lock().unwrap();
            inner
                .db
                .execute(
                    "INSERT INTO channels VALUES('mskdsp-lower','beta','linux-arm64',?1)",
                    [&old.id],
                )
                .unwrap();
            inner
                .db
                .execute(
                    "UPDATE packages SET ever_published=1,published=?1 WHERE id=?2",
                    params![timestamp(), old.id],
                )
                .unwrap();
        }
        drop(store);
        let store = SoftwareStore::new(root, 64).unwrap();
        for id in [&old.id, &next.id] {
            assert!(matches!(
                store.publish(id, "beta").await,
                Err(StoreError::Invalid(_))
            ));
        }
        let legacy = store.get_package(&old.id).await.unwrap();
        assert_eq!(legacy.channels, vec!["beta"]);
        assert!(
            store
                .list_packages()
                .await
                .unwrap()
                .iter()
                .any(|p| p.id == old.id)
        );
        assert_eq!(
            store.get_package(&next.id).await.unwrap().channels,
            vec!["ci", "stable"]
        );
        let manifest = store
            .manifest(
                "mskdsp-lower",
                "beta",
                Some("linux-arm64"),
                "https://example.test",
            )
            .await
            .unwrap();
        assert_eq!(manifest["version"], "1.0.0");
        assert!(store.download_file(&old.id, "lower.tar.gz").await.is_ok());
        assert!(
            store
                .withdraw(&old.id, "beta")
                .await
                .unwrap()
                .channels
                .is_empty()
        );
        assert!(
            store
                .manifest(
                    "mskdsp-lower",
                    "beta",
                    Some("linux-arm64"),
                    "https://example.test"
                )
                .await
                .is_err()
        );
        assert!(store.download_file(&old.id, "lower.tar.gz").await.is_ok());
    }
    #[tokio::test]
    async fn upper_and_lower_maintenance_branch_packages_can_publish_stable() {
        let dir = tempfile::tempdir().unwrap();
        let store = SoftwareStore::new(dir.path().into(), 64).unwrap();
        for product in ["mskdsp-upper", "mskdsp-lower"] {
            let mut m = metadata(b"patch");
            m.product = product.into();
            m.version = "1.0.1".into();
            m.source_ref = "release/1.0".into();
            m.source_sha = "a".repeat(40);
            if product == "mskdsp-upper" {
                m.platform = "windows-x86_64-nsis".into();
                m.signature = "signature".into();
            }
            let package = upload(&store, m, b"patch").await;
            assert!(matches!(
                store.publish(&package.id, "beta").await,
                Err(StoreError::Invalid(_))
            ));
            assert_eq!(
                store.publish(&package.id, "stable").await.unwrap().channels,
                vec!["stable"]
            );
        }
    }
    #[tokio::test]
    async fn rename_before_database_commit_recovers_on_restart() {
        let dir = tempfile::tempdir().unwrap();
        let store = SoftwareStore::new(dir.path().into(), 64).unwrap();
        let m = metadata(b"abc");
        let task = store.create_upload(m.clone()).await.unwrap();
        store
            .append_chunk(&task.id, 0, b"abc".to_vec())
            .await
            .unwrap();
        {
            let inner = store.inner.lock().unwrap();
            let target = inner.package_path(&m).unwrap();
            ensure_dir(target.parent().unwrap()).unwrap();
            fs::rename(inner.temporary(&task.id).unwrap(), target).unwrap();
        }
        drop(store);
        let store = SoftwareStore::new(dir.path().into(), 64).unwrap();
        assert_eq!(store.get_upload(&task.id).await.unwrap().state, "completed");
        assert_eq!(store.complete_upload(&task.id).await.unwrap().metadata, m);
    }
    #[tokio::test]
    async fn missing_temporary_file_and_corrupt_move_do_not_block_restart() {
        let dir = tempfile::tempdir().unwrap();
        let store = SoftwareStore::new(dir.path().into(), 64).unwrap();
        let a = store.create_upload(metadata(b"abc")).await.unwrap();
        let mut m = metadata(b"def");
        m.version = "2.0.0".into();
        let b = store.create_upload(m.clone()).await.unwrap();
        {
            let inner = store.inner.lock().unwrap();
            fs::remove_file(inner.temporary(&a.id).unwrap()).unwrap();
            let target = inner.package_path(&m).unwrap();
            ensure_dir(target.parent().unwrap()).unwrap();
            fs::write(target, b"bad").unwrap();
        }
        drop(store);
        let store = SoftwareStore::new(dir.path().into(), 64).unwrap();
        assert_eq!(store.get_upload(&a.id).await.unwrap().state, "failed");
        assert_eq!(store.get_upload(&b.id).await.unwrap().state, "failed");
        assert!(store.list_packages().await.unwrap().is_empty());
        assert_eq!(store.purge_expired(0).await.unwrap(), 2);
    }
    #[tokio::test]
    async fn expiry_task_count_and_tampered_publish_are_enforced() {
        let dir = tempfile::tempdir().unwrap();
        let store = SoftwareStore::new(dir.path().into(), 64).unwrap();
        for i in 0..TASK_LIMIT {
            let mut m = metadata(b"abc");
            m.version = format!("1.0.{i}");
            store.create_upload(m).await.unwrap();
        }
        let mut m = metadata(b"abc");
        m.version = "2.0.0".into();
        assert!(matches!(
            store.create_upload(m.clone()).await,
            Err(StoreError::Limit(_))
        ));
        assert_eq!(store.purge_expired(0).await.unwrap(), TASK_LIMIT as usize);
        let p = upload(&store, m, b"abc").await;
        let path = store
            .inner
            .lock()
            .unwrap()
            .package_path(&p.metadata)
            .unwrap();
        fs::write(path, b"bad").unwrap();
        assert!(store.publish(&p.id, "ci").await.is_err());
        assert!(store.get_package(&p.id).await.unwrap().channels.is_empty());
    }
    #[tokio::test]
    async fn upper_channel_switch_never_mixes_versions_and_can_return_to_history() {
        let dir = tempfile::tempdir().unwrap();
        let store = SoftwareStore::new(dir.path().into(), 64).unwrap();
        let upper = |version: &str, platform: &str| PackageMetadata {
            product: "mskdsp-upper".into(),
            version: version.into(),
            platform: platform.into(),
            file_name: "installer.exe".into(),
            signature: "signed".into(),
            ..metadata(b"abc")
        };
        let a = upload(&store, upper("1.0.0", "windows-x86_64"), b"abc").await;
        let b = upload(&store, upper("1.0.0", "windows-x86_64-nsis"), b"abc").await;
        let c = upload(&store, upper("2.0.0", "windows-x86_64-nsis"), b"abc").await;
        store.publish(&a.id, "stable").await.unwrap();
        store.publish(&b.id, "stable").await.unwrap();
        let m = store
            .manifest("mskdsp-upper", "stable", None, "https://example.test")
            .await
            .unwrap();
        assert_eq!(m["platforms"].as_object().unwrap().len(), 2);
        store.publish(&c.id, "stable").await.unwrap();
        let m = store
            .manifest("mskdsp-upper", "stable", None, "https://example.test")
            .await
            .unwrap();
        assert_eq!(m["version"], "2.0.0");
        assert_eq!(m["platforms"].as_object().unwrap().len(), 1);
        assert!(store.get_package(&a.id).await.unwrap().channels.is_empty());
        store.publish(&a.id, "stable").await.unwrap();
        assert_eq!(
            store
                .manifest("mskdsp-upper", "stable", None, "https://example.test")
                .await
                .unwrap()["version"],
            "1.0.0"
        );
    }
    #[tokio::test]
    async fn concurrent_same_identity_is_idempotent_and_stale_offset_is_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let store = SoftwareStore::new(dir.path().into(), 64).unwrap();
        let (a, b) = tokio::join!(
            store.create_upload(metadata(b"abc")),
            store.create_upload(metadata(b"abc"))
        );
        let id = a.unwrap().id;
        assert_eq!(id, b.unwrap().id);
        let (a, b) = tokio::join!(
            store.append_chunk(&id, 0, b"abc".to_vec()),
            store.append_chunk(&id, 0, b"abc".to_vec())
        );
        assert_eq!([a.is_ok(), b.is_ok()].into_iter().filter(|v| *v).count(), 1);
        store.complete_upload(&id).await.unwrap();
    }
    #[tokio::test]
    async fn independently_opened_stores_share_immutable_upload_identity() {
        let dir = tempfile::tempdir().unwrap();
        let first = SoftwareStore::new(dir.path().into(), 64).unwrap();
        let second = SoftwareStore::new(dir.path().into(), 64).unwrap();
        let (a, b) = tokio::join!(
            first.create_upload(metadata(b"abc")),
            second.create_upload(metadata(b"abc"))
        );
        assert_eq!(a.unwrap().id, b.unwrap().id);
        assert!(matches!(
            second.create_upload(metadata(b"def")).await,
            Err(StoreError::Conflict(_))
        ));
    }
    #[test]
    fn future_schema_and_missing_core_schema_are_rejected() {
        let dir = tempfile::tempdir().unwrap();
        drop(SoftwareStore::new(dir.path().into(), 64).unwrap());
        let db = Connection::open(dir.path().join("software.sqlite3")).unwrap();
        assert_eq!(
            db.query_row("PRAGMA user_version", [], |r| r.get::<_, u32>(0))
                .unwrap(),
            1
        );
        db.execute_batch("PRAGMA user_version=2").unwrap();
        drop(db);
        assert!(matches!(
            SoftwareStore::new(dir.path().into(), 64),
            Err(StoreError::Storage(_))
        ));
        let db = Connection::open(dir.path().join("software.sqlite3")).unwrap();
        db.execute_batch("PRAGMA user_version=1; DROP TABLE channels;")
            .unwrap();
        drop(db);
        assert!(matches!(
            SoftwareStore::new(dir.path().into(), 64),
            Err(StoreError::Storage(_))
        ));
        let db = Connection::open(dir.path().join("software.sqlite3")).unwrap();
        assert_eq!(
            db.query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='channels'",
                [],
                |r| r.get::<_, u32>(0)
            )
            .unwrap(),
            0
        );
    }
    #[test]
    fn missing_columns_are_rejected_without_repairing_the_database() {
        let dir = tempfile::tempdir().unwrap();
        drop(SoftwareStore::new(dir.path().into(), 64).unwrap());
        let db = Connection::open(dir.path().join("software.sqlite3")).unwrap();
        db.execute_batch("DROP TABLE channels; CREATE TABLE channels(product TEXT);")
            .unwrap();
        drop(db);
        assert!(matches!(
            SoftwareStore::new(dir.path().into(), 64),
            Err(StoreError::Storage(_))
        ));
    }
    #[tokio::test]
    async fn missing_database_with_existing_uploads_or_packages_is_rejected() {
        for directory in ["uploads", "packages"] {
            let dir = tempfile::tempdir().unwrap();
            fs::create_dir_all(dir.path().join(directory).join("existing")).unwrap();
            assert!(matches!(
                SoftwareStore::new(dir.path().into(), 64),
                Err(StoreError::Storage(_))
            ));
            assert!(!dir.path().join("software.sqlite3").exists());
        }
        let dir = tempfile::tempdir().unwrap();
        let store = SoftwareStore::new(dir.path().into(), 64).unwrap();
        upload(&store, metadata(b"abc"), b"abc").await;
        drop(store);
        fs::remove_file(dir.path().join("software.sqlite3")).unwrap();
        assert!(matches!(
            SoftwareStore::new(dir.path().into(), 64),
            Err(StoreError::Storage(_))
        ));
    }
    #[cfg(unix)]
    #[tokio::test]
    async fn symlink_package_paths_are_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();
        let store = SoftwareStore::new(dir.path().into(), 64).unwrap();
        std::os::unix::fs::symlink(outside.path(), dir.path().join("packages/mskdsp-lower"))
            .unwrap();
        let task = store.create_upload(metadata(b"abc")).await.unwrap();
        store
            .append_chunk(&task.id, 0, b"abc".to_vec())
            .await
            .unwrap();
        assert!(matches!(
            store.complete_upload(&task.id).await,
            Err(StoreError::Invalid(_))
        ));
        assert!(fs::read_dir(outside.path()).unwrap().next().is_none());
    }
}

use super::models::{DownloadFile, Package, PackageMetadata, StoreError, Upload};
use rusqlite::{Connection, OptionalExtension, params};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
};
use uuid::Uuid;

const CHUNK_LIMIT: usize = 1024 * 1024;
const PACKAGE_LIMIT: u64 = 8 * 1024 * 1024 * 1024;
const PLATFORM_PACKAGE_LIMIT: u64 = 128 * 1024 * 1024;
const TASK_LIMIT: u64 = 32;
#[derive(Clone)]
pub struct SoftwareStore {
    inner: Arc<Mutex<Inner>>,
}
struct Inner {
    root: PathBuf,
    db: Connection,
    limit: u64,
}
fn storage(_: impl std::fmt::Display) -> StoreError {
    StoreError::Storage("软件仓库存储操作失败".into())
}
fn now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
fn timestamp() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
}
fn safe_component(value: &str) -> bool {
    if value.is_empty()
        || value.len() > 180
        || value == "."
        || value == ".."
        || value.ends_with('.')
    {
        return false;
    }
    if !value
        .bytes()
        .all(|v| v.is_ascii_alphanumeric() || matches!(v, b'.' | b'-' | b'_' | b'+'))
    {
        return false;
    }
    let stem = value.split('.').next().unwrap_or("").to_ascii_uppercase();
    !matches!(
        stem.as_str(),
        "CON"
            | "PRN"
            | "AUX"
            | "NUL"
            | "COM1"
            | "COM2"
            | "COM3"
            | "COM4"
            | "COM5"
            | "COM6"
            | "COM7"
            | "COM8"
            | "COM9"
            | "LPT1"
            | "LPT2"
            | "LPT3"
            | "LPT4"
            | "LPT5"
            | "LPT6"
            | "LPT7"
            | "LPT8"
            | "LPT9"
    )
}
fn valid_id(id: &str) -> Result<(), StoreError> {
    if Uuid::parse_str(id).is_ok_and(|v| v.to_string() == id) {
        Ok(())
    } else {
        Err(StoreError::Invalid("无效的软件包或上传 ID".into()))
    }
}
fn hash_valid(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}
fn channel_valid(channel: &str) -> Result<(), StoreError> {
    if matches!(channel, "ci" | "beta" | "stable") {
        Ok(())
    } else {
        Err(StoreError::Invalid("无效发布通道".into()))
    }
}
pub(crate) fn platform_ci_identity(m: &PackageMetadata) -> Result<(), StoreError> {
    let invalid = || {
        StoreError::Invalid(
            "平台包必须为 main 的 linux-amd64 CI 构建，且版本、完整提交和构建时间一致（兼容历史 UTC 编号）".into(),
        )
    };
    if m.product != "mskdsp-platform"
        || m.platform != "linux-amd64"
        || m.channel != "ci"
        || m.source_ref != "main"
        || m.source_sha.len() != 40
        || !m
            .source_sha
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
        || !m.file_name.ends_with("binary.tar.gz")
    {
        return Err(invalid());
    }
    let moment = chrono::DateTime::parse_from_rfc3339(&m.build_time).map_err(|_| invalid())?;
    if moment.offset().local_minus_utc() != 0 || moment.timestamp_subsec_nanos() != 0 {
        return Err(invalid());
    }
    let version = semver::Version::parse(&m.version).map_err(|_| invalid())?;
    let pre = version.pre.as_str().split('.').collect::<Vec<_>>();
    let stamp = moment
        .with_timezone(&chrono::FixedOffset::east_opt(8 * 3600).unwrap())
        .format("%Y%m%dt%H%M%S")
        .to_string();
    // 新编号使用北京时间，历史 UTC 编号仍允许上传和读取。
    let legacy_stamp = moment.format("%Y%m%dt%H%M%S").to_string();
    if pre.len() != 4
        || pre[0] != "ci"
        || (pre[1] != stamp && pre[1] != legacy_stamp)
        || !pre[2..]
            .iter()
            .all(|v| !v.is_empty() && v.bytes().all(|b| b.is_ascii_digit()))
        || version.build.as_str() != format!("sha.{}", &m.source_sha[..7])
    {
        return Err(invalid());
    }
    Ok(())
}
fn validate(m: &PackageMetadata, limit: u64) -> Result<(), StoreError> {
    if !matches!(
        m.product.as_str(),
        "mskdsp-upper" | "mskdsp-lower" | "mskdsp-platform" | super::TESTBENCH_PRODUCT
    ) || !safe_component(&m.version)
        || !safe_component(&m.platform)
        || !safe_component(&m.file_name)
        || m.file_name == "SHA256SUMS"
        || !hash_valid(&m.sha256)
    {
        return Err(StoreError::Invalid(
            "安装包身份、文件名或 SHA256 无效".into(),
        ));
    }
    if m.size == 0
        || m.size > limit
        || m.product == "mskdsp-platform" && m.size > PLATFORM_PACKAGE_LIMIT
    {
        return Err(StoreError::Limit("安装包大小超过限制或为空".into()));
    }
    if m.signature.len() > 32768
        || m.notes.len() > 65536
        || m.source_repository.len() > 2048
        || m.source_ref.len() > 512
        || m.source_sha.len() > 128
        || m.build_time.len() > 64
        || m.channel.len() > 32
    {
        return Err(StoreError::Limit("版本元数据过长".into()));
    }
    if m.product == "mskdsp-platform" {
        platform_ci_identity(m)?;
    } else if m.product == "mskdsp-upper" {
        if semver::Version::parse(&m.version).is_err() {
            return Err(StoreError::Invalid("上位机版本必须是合法 SemVer".into()));
        }
        if !matches!(
            m.platform.as_str(),
            "windows-x86_64"
                | "windows-x86_64-nsis"
                | "windows-aarch64"
                | "windows-aarch64-nsis"
                | "linux-x86_64"
                | "linux-aarch64"
                | "darwin-x86_64"
                | "darwin-aarch64"
        ) || m.signature.trim().is_empty()
        {
            return Err(StoreError::Invalid("上位机架构不支持或缺少签名".into()));
        }
    } else if m.product == super::TESTBENCH_PRODUCT {
        if semver::Version::parse(&m.version).is_err()
            || m.platform != "windows-x86_64-nsis"
            || m.signature.trim().is_empty()
            || super::testbench_channel(&m.channel).is_none()
        {
            return Err(StoreError::Invalid(
                "测试上位机须使用合法 SemVer、Windows x64 NSIS 架构、更新签名和 CI 或 Stable 通道"
                    .into(),
            ));
        }
    } else if !matches!(m.platform.as_str(), "linux-arm64" | "linux-x64")
        || !safe_component(&m.package_version)
        || !m.image_id.strip_prefix("sha256:").is_some_and(hash_valid)
    {
        return Err(StoreError::Invalid(
            "下位机架构、包版本或镜像摘要无效".into(),
        ));
    }
    Ok(())
}

fn validate_new_testbench_version(m: &PackageMetadata) -> Result<(), StoreError> {
    let invalid = || {
        StoreError::Invalid(
            "测试上位机 Stable 版本须为 x.y.z，CI 版本须为 x.y.z-ci.<运行编号>.<尝试编号>".into(),
        )
    };
    let version = semver::Version::parse(&m.version).map_err(|_| invalid())?;
    let positive = |number: &str| number.parse::<u64>().is_ok_and(|value| value > 0);
    let valid = if m.channel == "ci" {
        let mut parts = version.pre.as_str().split('.');
        parts.next() == Some("ci")
            && parts.next().is_some_and(positive)
            && parts.next().is_some_and(positive)
            && parts.next().is_none()
    } else {
        version.pre.is_empty()
    };
    if valid && version.build.is_empty() {
        Ok(())
    } else {
        Err(invalid())
    }
}

fn validate_testbench_release_channel(
    m: &PackageMetadata,
    channel: &str,
) -> Result<(), StoreError> {
    if m.product == super::TESTBENCH_PRODUCT
        && super::testbench_channel(&m.channel) != Some(channel)
    {
        return Err(StoreError::Invalid(
            "测试上位机仅可在安装包所属通道发布或撤回".into(),
        ));
    }
    Ok(())
}
// 拒绝路径中所有已存在的符号链接，包括配置根目录的祖先路径。
fn reject_links(path: &Path) -> Result<(), StoreError> {
    let mut current = PathBuf::new();
    for component in path.components() {
        current.push(component.as_os_str());
        // Windows 逐字驱动器前缀（\\?\C:）须拼接根组件后才能查询。
        if matches!(component, std::path::Component::Prefix(_)) {
            continue;
        }
        match fs::symlink_metadata(&current) {
            Ok(meta) => {
                if meta.file_type().is_symlink() {
                    return Err(StoreError::Invalid("软件仓库路径不能包含符号链接".into()));
                }
                #[cfg(windows)]
                {
                    use std::os::windows::fs::MetadataExt;
                    if meta.file_attributes() & 0x400 != 0 {
                        return Err(StoreError::Invalid("软件仓库路径不能包含重解析点".into()));
                    }
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(storage(e)),
        }
    }
    Ok(())
}
fn ensure_dir(path: &Path) -> Result<(), StoreError> {
    reject_links(path)?;
    fs::create_dir_all(path).map_err(storage)?;
    reject_links(path)
}
fn sync_directories(path: &Path, root: &Path) -> Result<(), StoreError> {
    // 在 SQLite 记录完成前，先持久化重命名和目录项。
    #[cfg(unix)]
    {
        let mut directory = path.parent();
        while let Some(parent) = directory {
            File::open(parent)
                .map_err(storage)?
                .sync_all()
                .map_err(storage)?;
            if parent == root {
                break;
            }
            directory = parent.parent();
        }
    }
    #[cfg(not(unix))]
    let _ = (path, root);
    Ok(())
}
fn digest_file(path: &Path) -> Result<(u64, String), StoreError> {
    reject_links(path)?;
    let mut file = File::open(path).map_err(storage)?;
    if !file.metadata().map_err(storage)?.is_file() {
        return Err(StoreError::Invalid("软件包必须是普通文件".into()));
    }
    let mut hash = Sha256::new();
    let mut size = 0;
    let mut buffer = [0u8; 65536];
    loop {
        let n = file.read(&mut buffer).map_err(storage)?;
        if n == 0 {
            break;
        }
        hash.update(&buffer[..n]);
        size += n as u64;
    }
    Ok((size, format!("{:x}", hash.finalize())))
}
fn verify(path: &Path, m: &PackageMetadata) -> Result<(), StoreError> {
    let (size, hash) = digest_file(path)?;
    if size != m.size || hash != m.sha256 {
        Err(StoreError::Conflict("安装包大小或 SHA256 校验失败".into()))
    } else {
        Ok(())
    }
}
impl SoftwareStore {
    pub fn new(root: PathBuf, max_package_bytes: u64) -> Result<Self, StoreError> {
        ensure_dir(&root)?;
        let root = fs::canonicalize(root).map_err(storage)?;
        ensure_dir(&root.join("uploads"))?;
        ensure_dir(&root.join("packages"))?;
        let db_path = root.join("software.sqlite3");
        reject_links(&db_path)?;
        reject_links(&root.join("software.sqlite3-wal"))?;
        reject_links(&root.join("software.sqlite3-shm"))?;
        let existing_database = db_path.try_exists().map_err(storage)?;
        if !existing_database {
            for directory in ["uploads", "packages"] {
                if fs::read_dir(root.join(directory))
                    .map_err(storage)?
                    .next()
                    .is_some()
                {
                    return Err(StoreError::Storage(
                        "软件仓库数据库缺失但已有历史文件，请先恢复一致的备份".into(),
                    ));
                }
            }
        }
        let flags = if existing_database {
            rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE
        } else {
            rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE | rusqlite::OpenFlags::SQLITE_OPEN_CREATE
        };
        let db = Connection::open_with_flags(&db_path, flags).map_err(storage)?;
        db.busy_timeout(std::time::Duration::from_secs(5))
            .map_err(storage)?;
        let schema_version: u32 = db
            .query_row("PRAGMA user_version", [], |row| row.get(0))
            .map_err(storage)?;
        if schema_version > 1 {
            return Err(StoreError::Storage(
                "软件仓库数据库版本高于当前程序支持版本".into(),
            ));
        }
        if existing_database {
            for (table, columns) in [
                (
                    "uploads",
                    "id,metadata,product,version,platform,received,state,package_id,created,updated",
                ),
                (
                    "packages",
                    "id,metadata,product,version,platform,created,published,ever_published",
                ),
                ("channels", "product,channel,platform,package_id"),
            ] {
                let exists: bool = db
                    .query_row(
                        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1)",
                        [table],
                        |row| row.get(0),
                    )
                    .map_err(storage)?;
                if !exists
                    || db
                        .prepare(&format!("SELECT {columns} FROM {table} LIMIT 0"))
                        .is_err()
                {
                    return Err(StoreError::Storage(
                        "软件仓库数据库结构不完整，请先恢复一致的备份".into(),
                    ));
                }
            }
        }
        db.execute_batch(
            "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;",
        )
        .map_err(storage)?;
        if !existing_database {
            db.execute_batch("BEGIN IMMEDIATE;
              CREATE TABLE uploads(id TEXT PRIMARY KEY, metadata TEXT NOT NULL, product TEXT NOT NULL, version TEXT NOT NULL, platform TEXT NOT NULL, received INTEGER NOT NULL, state TEXT NOT NULL, package_id TEXT, created INTEGER NOT NULL, updated INTEGER NOT NULL);
              CREATE TABLE packages(id TEXT PRIMARY KEY, metadata TEXT NOT NULL, product TEXT NOT NULL, version TEXT NOT NULL, platform TEXT NOT NULL, created TEXT NOT NULL, published TEXT, ever_published INTEGER NOT NULL DEFAULT 0, UNIQUE(product,version,platform));
              CREATE TABLE channels(product TEXT NOT NULL, channel TEXT NOT NULL, platform TEXT NOT NULL, package_id TEXT NOT NULL REFERENCES packages(id), PRIMARY KEY(product,channel,platform));
              CREATE INDEX upload_identity ON uploads(product,version,platform);
              CREATE UNIQUE INDEX active_upload_identity ON uploads(product,version,platform) WHERE state IN ('uploading','completed');
              PRAGMA user_version=1; COMMIT;").map_err(storage)?;
        } else {
            db.execute_batch("BEGIN IMMEDIATE;
              CREATE INDEX IF NOT EXISTS upload_identity ON uploads(product,version,platform);
              CREATE UNIQUE INDEX IF NOT EXISTS active_upload_identity ON uploads(product,version,platform) WHERE state IN ('uploading','completed');
              PRAGMA user_version=1; COMMIT;").map_err(storage)?;
        }
        let mut inner = Inner {
            root,
            db,
            limit: max_package_bytes.min(PACKAGE_LIMIT),
        };
        let ids = {
            let mut stmt = inner
                .db
                .prepare("SELECT id FROM uploads WHERE state='uploading'")
                .map_err(storage)?;
            stmt.query_map([], |r| r.get::<_, String>(0))
                .map_err(storage)?
                .collect::<Result<Vec<_>, _>>()
                .map_err(storage)?
        };
        for id in ids {
            let upload = inner.upload(&id)?;
            if inner.package_path(&upload.metadata)?.exists() {
                match inner.complete(&id) {
                    Ok(_) | Err(StoreError::Conflict(_)) => {}
                    Err(error) => return Err(error),
                }
            } else {
                match inner.refresh(&id) {
                    Ok(_) | Err(StoreError::Conflict(_)) => {}
                    Err(error) => return Err(error),
                }
            }
        }
        Ok(Self {
            inner: Arc::new(Mutex::new(inner)),
        })
    }
    async fn run<T, F>(&self, f: F) -> Result<T, StoreError>
    where
        T: Send + 'static,
        F: FnOnce(&mut Inner) -> Result<T, StoreError> + Send + 'static,
    {
        let inner = self.inner.clone();
        tokio::task::spawn_blocking(move || {
            let mut guard = inner.lock().map_err(storage)?;
            f(&mut guard)
        })
        .await
        .map_err(storage)?
    }
    pub async fn create_upload(&self, metadata: PackageMetadata) -> Result<Upload, StoreError> {
        self.run(move |s| s.create(metadata)).await
    }
    pub async fn get_upload(&self, id: &str) -> Result<Upload, StoreError> {
        let id = id.to_owned();
        self.run(move |s| s.refresh(&id)).await
    }
    // 认证读取不刷新上传进度，避免越权请求改变其他产品任务状态。
    pub(super) async fn upload_metadata(&self, id: &str) -> Result<PackageMetadata, StoreError> {
        let id = id.to_owned();
        self.run(move |s| Ok(s.upload(&id)?.metadata)).await
    }
    pub async fn append_chunk(
        &self,
        id: &str,
        offset: u64,
        data: Vec<u8>,
    ) -> Result<Upload, StoreError> {
        let id = id.to_owned();
        self.run(move |s| s.append(&id, offset, data)).await
    }
    pub async fn complete_upload(&self, id: &str) -> Result<Package, StoreError> {
        let id = id.to_owned();
        self.run(move |s| s.complete(&id)).await
    }
    pub async fn list_packages(&self) -> Result<Vec<Package>, StoreError> {
        self.run(|s| s.packages()).await
    }
    pub async fn get_package(&self, id: &str) -> Result<Package, StoreError> {
        let id = id.to_owned();
        self.run(move |s| s.package(&id)).await
    }
    pub async fn publish(&self, id: &str, channel: &str) -> Result<Package, StoreError> {
        let id = id.to_owned();
        let channel = channel.to_owned();
        self.run(move |s| s.publish(&id, &channel)).await
    }
    pub async fn withdraw(&self, id: &str, channel: &str) -> Result<Package, StoreError> {
        let id = id.to_owned();
        let channel = channel.to_owned();
        self.run(move |s| {
            channel_valid(&channel)?;
            let package = s.package(&id)?;
            if package.metadata.product == "mskdsp-platform" {
                return Err(StoreError::Invalid(
                    "平台候选包不支持发布或撤回，请到程序更新页手动升级".into(),
                ));
            }
            validate_testbench_release_channel(&package.metadata, &channel)?;
            s.db.execute(
                "DELETE FROM channels WHERE channel=?1 AND package_id=?2",
                params![channel, id],
            )
            .map_err(storage)?;
            s.package(&id)
        })
        .await
    }
    pub async fn manifest(
        &self,
        product: &str,
        channel: &str,
        platform: Option<&str>,
        base_url: &str,
    ) -> Result<serde_json::Value, StoreError> {
        let product = product.to_owned();
        let channel = channel.to_owned();
        let platform = platform.map(str::to_owned);
        let base = base_url.to_owned();
        self.run(move |s| s.manifest(&product, &channel, platform.as_deref(), &base))
            .await
    }
    pub async fn download_file(&self, id: &str, name: &str) -> Result<DownloadFile, StoreError> {
        let id = id.to_owned();
        let name = name.to_owned();
        self.run(move |s| {
            let p = s.public_package(&id)?;
            if p.metadata.file_name != name {
                return Err(StoreError::NotFound("安装包文件不存在".into()));
            }
            let path = s.package_path(&p.metadata)?;
            reject_links(&path)?;
            let meta = fs::metadata(&path).map_err(storage)?;
            if !meta.is_file() || meta.len() != p.metadata.size {
                return Err(StoreError::Storage("安装包文件不完整".into()));
            }
            Ok(DownloadFile {
                path,
                size: p.metadata.size,
                sha256: p.metadata.sha256,
                file_name: name,
            })
        })
        .await
    }
    pub async fn checksum(&self, id: &str) -> Result<String, StoreError> {
        let id = id.to_owned();
        self.run(move |s| {
            let p = s.public_package(&id)?;
            if p.metadata.product != "mskdsp-lower" {
                return Err(StoreError::NotFound("该软件包不提供 SHA256SUMS".into()));
            }
            Ok(format!("{}  {}\n", p.metadata.sha256, p.metadata.file_name))
        })
        .await
    }
    pub async fn purge_expired(&self, retention_seconds: u64) -> Result<usize, StoreError> {
        self.run(move |s| s.purge(retention_seconds)).await
    }
}
impl Inner {
    fn temporary(&self, id: &str) -> Result<PathBuf, StoreError> {
        valid_id(id)?;
        let path = self.root.join("uploads").join(id).join("package.part");
        reject_links(&path)?;
        Ok(path)
    }
    fn package_path(&self, m: &PackageMetadata) -> Result<PathBuf, StoreError> {
        validate(m, PACKAGE_LIMIT)?;
        let path = self
            .root
            .join("packages")
            .join(&m.product)
            .join(&m.version)
            .join(&m.platform)
            .join(&m.file_name);
        reject_links(&path)?;
        Ok(path)
    }
    fn upload(&self, id: &str) -> Result<Upload, StoreError> {
        valid_id(id)?;
        let row=self.db.query_row("SELECT metadata,received,state,package_id,created,updated FROM uploads WHERE id=?1",[id],|r|Ok((r.get::<_,String>(0)?,r.get::<_,u64>(1)?,r.get::<_,String>(2)?,r.get::<_,Option<String>>(3)?,r.get::<_,u64>(4)?,r.get::<_,u64>(5)?))).optional().map_err(storage)?.ok_or_else(||StoreError::NotFound("上传任务不存在".into()))?;
        Ok(Upload {
            id: id.into(),
            metadata: serde_json::from_str(&row.0).map_err(storage)?,
            received_bytes: row.1,
            state: row.2,
            package_id: row.3,
            created_at: row.4,
            updated_at: row.5,
        })
    }
    fn refresh(&mut self, id: &str) -> Result<Upload, StoreError> {
        let mut u = self.upload(id)?;
        if u.state == "uploading" {
            let path = self.temporary(id)?;
            let actual = match fs::metadata(&path) {
                Ok(metadata) if metadata.is_file() => metadata.len(),
                Ok(_) => return Err(StoreError::Invalid("上传临时路径不是普通文件".into())),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                    self.fail(id)?;
                    return self.upload(id);
                }
                Err(error) => return Err(storage(error)),
            };
            if actual > u.metadata.size {
                self.fail(id)?;
                return Err(StoreError::Conflict("临时文件超过声明大小".into()));
            }
            if actual != u.received_bytes {
                self.db
                    .execute(
                        "UPDATE uploads SET received=?1 WHERE id=?2",
                        params![actual, id],
                    )
                    .map_err(storage)?;
                u.received_bytes = actual;
            }
        }
        Ok(u)
    }
    fn create(&mut self, m: PackageMetadata) -> Result<Upload, StoreError> {
        validate(&m, self.limit)?;
        let existing=self.db.query_row("SELECT id,metadata FROM uploads WHERE product=?1 AND version=?2 AND platform=?3 AND state IN ('uploading','completed') ORDER BY created DESC LIMIT 1",params![m.product,m.version,m.platform],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?))).optional().map_err(storage)?;
        if let Some((id, json)) = existing {
            let old: PackageMetadata = serde_json::from_str(&json).map_err(storage)?;
            if old != m {
                return Err(StoreError::Conflict(
                    "相同产品、版本和架构已有不同元数据".into(),
                ));
            }
            return self.refresh(&id);
        }
        // 历史 Stable 元数据仍可续传，版本格式限制只用于创建新上传任务。
        if m.product == super::TESTBENCH_PRODUCT {
            validate_new_testbench_version(&m)?;
        }
        let count: u64 = self
            .db
            .query_row(
                "SELECT COUNT(*) FROM uploads WHERE state='uploading'",
                [],
                |r| r.get(0),
            )
            .map_err(storage)?;
        if count >= TASK_LIMIT {
            return Err(StoreError::Limit("未完成上传任务数量已达上限".into()));
        }
        let id = Uuid::new_v4().to_string();
        let path = self.temporary(&id)?;
        ensure_dir(path.parent().unwrap())?;
        OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
            .map_err(storage)?
            .sync_all()
            .map_err(storage)?;
        sync_directories(&path, &self.root)?;
        let at = now();
        let result = self.db.execute(
            "INSERT INTO uploads VALUES(?1,?2,?3,?4,?5,0,'uploading',NULL,?6,?6)",
            params![
                id,
                serde_json::to_string(&m).map_err(storage)?,
                m.product,
                m.version,
                m.platform,
                at
            ],
        );
        if let Err(e) = result {
            let _ = fs::remove_file(path);
            if e.sqlite_error_code() == Some(rusqlite::ErrorCode::ConstraintViolation) {
                // 另一个独立连接可能先写入同一身份，重新读取其已提交元数据。
                return self.create(m);
            }
            return Err(storage(e));
        }
        self.upload(&id)
    }
    fn append(&mut self, id: &str, offset: u64, data: Vec<u8>) -> Result<Upload, StoreError> {
        if data.is_empty() || data.len() > CHUNK_LIMIT {
            return Err(StoreError::Limit("上传块必须为 1 字节至 1 MiB".into()));
        }
        let u = self.refresh(id)?;
        if u.state != "uploading" {
            return Err(StoreError::Conflict("上传任务不可继续写入".into()));
        }
        if offset != u.received_bytes {
            return Err(StoreError::Conflict(
                "上传偏移不匹配，请重新查询上传状态".into(),
            ));
        }
        let end = offset
            .checked_add(data.len() as u64)
            .ok_or_else(|| StoreError::Limit("上传偏移溢出".into()))?;
        if end > u.metadata.size || end > self.limit {
            return Err(StoreError::Limit("上传数据超过声明大小".into()));
        }
        let path = self.temporary(id)?;
        let mut file = OpenOptions::new().write(true).open(path).map_err(storage)?;
        file.seek(SeekFrom::Start(offset)).map_err(storage)?;
        file.write_all(&data).map_err(storage)?;
        file.sync_all().map_err(storage)?;
        self.db
            .execute(
                "UPDATE uploads SET received=?1,updated=?2 WHERE id=?3",
                params![end, now(), id],
            )
            .map_err(storage)?;
        self.upload(id)
    }
    fn fail(&self, id: &str) -> Result<(), StoreError> {
        self.db
            .execute(
                "UPDATE uploads SET state='failed',updated=?1 WHERE id=?2",
                params![now(), id],
            )
            .map_err(storage)?;
        Ok(())
    }
    fn complete(&mut self, id: &str) -> Result<Package, StoreError> {
        let u = self.upload(id)?;
        if u.state == "completed" {
            return self.package(u.package_id.as_deref().unwrap_or(id));
        }
        if u.state != "uploading" {
            return Err(StoreError::Conflict("上传任务不可完成".into()));
        }
        let target = self.package_path(&u.metadata)?;
        let temp = self.temporary(id)?;
        let source = if target.exists() {
            target.as_path()
        } else {
            temp.as_path()
        };
        if let Err(error) = verify(source, &u.metadata) {
            self.fail(id)?;
            return Err(error);
        }
        ensure_dir(target.parent().unwrap())?;
        if !target.exists() {
            fs::rename(&temp, &target).map_err(storage)?;
        } else if temp.exists() {
            fs::remove_file(&temp).map_err(storage)?;
        }
        sync_directories(&target, &self.root)?;
        sync_directories(&temp, &self.root)?;
        let tx = self.db.transaction().map_err(storage)?;
        tx.execute("INSERT INTO packages(id,metadata,product,version,platform,created) VALUES(?1,?2,?3,?4,?5,?6) ON CONFLICT(id) DO NOTHING",params![id,serde_json::to_string(&u.metadata).map_err(storage)?,u.metadata.product,u.metadata.version,u.metadata.platform,timestamp()]).map_err(storage)?;
        tx.execute(
            "UPDATE uploads SET state='completed',received=?1,package_id=?2,updated=?3 WHERE id=?2",
            params![u.metadata.size, id, now()],
        )
        .map_err(storage)?;
        tx.commit().map_err(storage)?;
        self.package(id)
    }
    fn package(&self, id: &str) -> Result<Package, StoreError> {
        valid_id(id)?;
        let row = self
            .db
            .query_row(
                "SELECT metadata,created,published,ever_published FROM packages WHERE id=?1",
                [id],
                |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, Option<String>>(2)?,
                        r.get::<_, bool>(3)?,
                    ))
                },
            )
            .optional()
            .map_err(storage)?
            .ok_or_else(|| StoreError::NotFound("软件包不存在".into()))?;
        let channels = {
            let mut stmt = self
                .db
                .prepare("SELECT channel FROM channels WHERE package_id=?1 ORDER BY channel")
                .map_err(storage)?;
            stmt.query_map([id], |r| r.get::<_, String>(0))
                .map_err(storage)?
                .collect::<Result<Vec<_>, _>>()
                .map_err(storage)?
        };
        Ok(Package {
            id: id.into(),
            metadata: serde_json::from_str(&row.0).map_err(storage)?,
            created_at: row.1,
            published_at: row.2,
            channels,
            ever_published: row.3,
        })
    }
    fn packages(&self) -> Result<Vec<Package>, StoreError> {
        let ids = {
            let mut stmt = self
                .db
                .prepare("SELECT id FROM packages ORDER BY created DESC,id")
                .map_err(storage)?;
            stmt.query_map([], |r| r.get::<_, String>(0))
                .map_err(storage)?
                .collect::<Result<Vec<_>, _>>()
                .map_err(storage)?
        };
        ids.into_iter().map(|id| self.package(&id)).collect()
    }
    fn public_package(&self, id: &str) -> Result<Package, StoreError> {
        let p = self.package(id)?;
        if !p.ever_published || p.metadata.product == "mskdsp-platform" {
            return Err(StoreError::NotFound("软件包尚未发布".into()));
        }
        Ok(p)
    }
    fn publish(&mut self, id: &str, channel: &str) -> Result<Package, StoreError> {
        channel_valid(channel)?;
        if channel == "beta" {
            return Err(StoreError::Invalid(
                "Beta 已停止发布，请选择 CI 或 Stable；历史 Beta 仍可查询和撤回".into(),
            ));
        }
        let p = self.package(id)?;
        if p.metadata.product == "mskdsp-platform" {
            return Err(StoreError::Invalid(
                "平台候选包不支持发布或撤回，请到程序更新页手动升级".into(),
            ));
        }
        if p.metadata.platform == "linux-x64" && channel != "ci" {
            return Err(StoreError::Invalid("下位机 x64 仅支持 CI 通道".into()));
        }
        validate_testbench_release_channel(&p.metadata, channel)?;
        verify(&self.package_path(&p.metadata)?, &p.metadata)?;
        let tx = self.db.transaction().map_err(storage)?;
        if p.metadata.product == super::TESTBENCH_PRODUCT && channel == "ci" {
            let current: Option<String> = tx.query_row(
                "SELECT p.version FROM channels c JOIN packages p ON p.id=c.package_id WHERE c.product=?1 AND c.channel=?2 AND c.platform=?3",
                params![p.metadata.product, channel, p.metadata.platform],
                |row| row.get(0),
            ).optional().map_err(storage)?;
            if let Some(current) = current {
                let current = semver::Version::parse(&current).map_err(storage)?;
                let candidate = semver::Version::parse(&p.metadata.version).map_err(storage)?;
                if candidate < current {
                    return Err(StoreError::Conflict(
                        "测试上位机 CI 通道已推荐更新的版本".into(),
                    ));
                }
            }
        }
        if p.metadata.product == "mskdsp-upper" {
            tx.execute("DELETE FROM channels WHERE product=?1 AND channel=?2 AND package_id IN (SELECT id FROM packages WHERE version<>?3)",params![p.metadata.product,channel,p.metadata.version]).map_err(storage)?;
        }
        tx.execute("INSERT INTO channels VALUES(?1,?2,?3,?4) ON CONFLICT(product,channel,platform) DO UPDATE SET package_id=excluded.package_id",params![p.metadata.product,channel,p.metadata.platform,id]).map_err(storage)?;
        tx.execute(
            "UPDATE packages SET ever_published=1,published=COALESCE(published,?1) WHERE id=?2",
            params![timestamp(), id],
        )
        .map_err(storage)?;
        tx.commit().map_err(storage)?;
        self.package(id)
    }
    fn manifest(
        &self,
        product: &str,
        channel: &str,
        platform: Option<&str>,
        base: &str,
    ) -> Result<serde_json::Value, StoreError> {
        channel_valid(channel)?;
        if product == super::TESTBENCH_PRODUCT && !matches!(channel, "ci" | "stable") {
            return Err(StoreError::Invalid(
                "测试上位机仅支持 CI 和 Stable 通道".into(),
            ));
        }
        let mut stmt = self
            .db
            .prepare(
                "SELECT package_id FROM channels WHERE product=?1 AND channel=?2 ORDER BY platform",
            )
            .map_err(storage)?;
        let ids = stmt
            .query_map(params![product, channel], |r| r.get::<_, String>(0))
            .map_err(storage)?
            .collect::<Result<Vec<_>, _>>()
            .map_err(storage)?;
        let packages = ids
            .into_iter()
            .map(|id| self.package(&id))
            .collect::<Result<Vec<_>, _>>()?;
        if product == super::TESTBENCH_PRODUCT && packages.is_empty() {
            return Err(StoreError::NotFound("通道尚未发布版本".into()));
        }
        let url =
            url::Url::parse(base).map_err(|_| StoreError::Invalid("公共下载地址无效".into()))?;
        if !matches!(url.scheme(), "http" | "https")
            || url.host_str().is_none()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
        {
            return Err(StoreError::Invalid("公共下载地址无效".into()));
        }
        let base = url.as_str().trim_end_matches('/');
        let download =
            |p: &Package, name: &str| format!("{base}/software/packages/{}/{name}", p.id);
        if matches!(product, "mskdsp-upper" | super::TESTBENCH_PRODUCT) {
            let p = packages
                .first()
                .ok_or_else(|| StoreError::NotFound("通道尚未发布版本".into()))?;
            let mut platforms = serde_json::Map::new();
            for p in &packages {
                platforms.insert(p.metadata.platform.clone(),serde_json::json!({"url":download(p,&p.metadata.file_name),"signature":p.metadata.signature}));
            }
            Ok(
                serde_json::json!({"version":p.metadata.version,"notes":p.metadata.notes,"pub_date":p.published_at,"platforms":platforms}),
            )
        } else if product == "mskdsp-lower" {
            let platform = platform.unwrap_or("linux-arm64");
            if !matches!(platform, "linux-arm64" | "linux-x64")
                || platform == "linux-x64" && channel != "ci"
            {
                return Err(StoreError::Invalid("下位机架构或通道无效".into()));
            }
            let p = packages
                .iter()
                .find(|p| p.metadata.platform == platform)
                .ok_or_else(|| StoreError::NotFound("通道尚未发布版本".into()))?;
            Ok(
                serde_json::json!({"schema_version":1,"product":"mskdsp-lower","channel":channel,"platform":platform,"version":p.metadata.version,"package_version":p.metadata.package_version,"image_id":p.metadata.image_id,"published_at":p.published_at,"source":{"repository":p.metadata.source_repository,"ref":p.metadata.source_ref,"sha":p.metadata.source_sha},"asset":{"name":p.metadata.file_name,"url":download(p,&p.metadata.file_name),"sha256":p.metadata.sha256,"size":p.metadata.size},"checksum":{"name":"SHA256SUMS","url":download(p,"SHA256SUMS")}}),
            )
        } else {
            Err(StoreError::Invalid("未知软件产品".into()))
        }
    }
    fn purge(&mut self, retention: u64) -> Result<usize, StoreError> {
        let cutoff = now().saturating_sub(retention);
        let ids = {
            let mut stmt = self
                .db
                .prepare(
                    "SELECT id FROM uploads WHERE state IN ('uploading','failed') AND updated<=?1",
                )
                .map_err(storage)?;
            stmt.query_map([cutoff], |r| r.get::<_, String>(0))
                .map_err(storage)?
                .collect::<Result<Vec<_>, _>>()
                .map_err(storage)?
        };
        for id in &ids {
            let path = self.temporary(id)?;
            if path.exists() {
                fs::remove_file(path).map_err(storage)?;
            }
            self.db
                .execute(
                    "UPDATE uploads SET state='expired',updated=?1 WHERE id=?2",
                    params![now(), id],
                )
                .map_err(storage)?;
        }
        Ok(ids.len())
    }
}
