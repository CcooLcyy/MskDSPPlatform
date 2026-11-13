use super::tests::{metadata, upload};
use super::*;
use std::collections::BTreeSet;

const CONTENT: &[u8] = b"installation package";
const OLD: &str = "2000-01-01T00:00:00Z";

fn lower_ci(job: u32) -> PackageMetadata {
    let version = format!("1.0.0-ci-p1-b1-j{job}-aaaaaaa");
    let package_version = format!("{version}-linux-arm64");
    PackageMetadata {
        file_name: format!("mskdsp-{package_version}"),
        package_version,
        version,
        source_repository: "https://example.test/releases/lower".into(),
        source_ref: "master".into(),
        source_sha: "a".repeat(40),
        build_time: "2026-10-18T09:42:36Z".into(),
        channel: "ci".into(),
        ..metadata(CONTENT)
    }
}

async fn age(store: &SoftwareStore, package: &Package) {
    let id = package.id.clone();
    store
        .run(move |inner| {
            let count = inner
                .db
                .execute(
                    "UPDATE packages SET created=?1 WHERE id=?2",
                    params![OLD, id],
                )
                .map_err(storage)?;
            assert_eq!(count, 1);
            Ok(())
        })
        .await
        .unwrap();
}

async fn candidates(store: &SoftwareStore) -> BTreeSet<String> {
    store
        .preview_cleanup(30)
        .await
        .unwrap()
        .packages
        .into_iter()
        .map(|package| package.id)
        .collect()
}

#[tokio::test]
async fn cleanup_filters_by_age_identity_and_current_channel_references() {
    let dir = tempfile::tempdir().unwrap();
    let store = SoftwareStore::new(dir.path().into(), 1024).unwrap();

    let unpublished_ci = upload(&store, lower_ci(1), CONTENT).await;
    let previous_ci = upload(&store, lower_ci(2), CONTENT).await;
    let current_ci = upload(&store, lower_ci(3), CONTENT).await;
    let previous_stable = upload(&store, lower_ci(4), CONTENT).await;
    let legacy = upload(&store, lower_ci(5), CONTENT).await;
    let recent_ci = upload(&store, lower_ci(6), CONTENT).await;
    let mut unknown = lower_ci(7);
    unknown.channel.clear();
    let unknown = upload(&store, unknown, CONTENT).await;
    let mut stable = metadata(CONTENT);
    stable.channel = "stable".into();
    let stable = upload(&store, stable, CONTENT).await;
    let mut beta = metadata(CONTENT);
    beta.channel = "beta".into();
    beta.version = "1.0.0-beta.1".into();
    let beta = upload(&store, beta, CONTENT).await;

    store.publish(&previous_ci.id, "ci").await.unwrap();
    store.publish(&current_ci.id, "ci").await.unwrap();
    store.publish(&previous_stable.id, "stable").await.unwrap();
    store.withdraw(&previous_stable.id, "stable").await.unwrap();
    store.publish(&previous_stable.id, "ci").await.unwrap();
    store.publish(&current_ci.id, "ci").await.unwrap();
    let legacy_id = legacy.id.clone();
    store
        .run(move |inner| {
            inner
                .db
                .execute(
                    "UPDATE packages SET ever_published=1,published=?1 WHERE id=?2",
                    params![OLD, legacy_id],
                )
                .map_err(storage)?;
            Ok(())
        })
        .await
        .unwrap();
    store.publish(&legacy.id, "ci").await.unwrap();
    store.publish(&current_ci.id, "ci").await.unwrap();

    for package in [
        &unpublished_ci,
        &previous_ci,
        &current_ci,
        &previous_stable,
        &legacy,
        &unknown,
        &stable,
        &beta,
    ] {
        age(&store, package).await;
    }

    let eligible = BTreeSet::from([
        unpublished_ci.id.clone(),
        previous_ci.id.clone(),
        legacy.id.clone(),
    ]);
    assert_eq!(candidates(&store).await, eligible);
    assert_eq!(store.cleanup_settings().await.unwrap(), None);
    assert!(store.cleanup_scheduled().await.unwrap().is_none());

    let result = store
        .cleanup_once(30, eligible.into_iter().collect())
        .await
        .unwrap();
    assert_eq!(result.deleted, 3);
    assert_eq!(result.freed_bytes, 3 * CONTENT.len() as u64);
    assert_eq!(result.pending, 0);

    for package in [&unpublished_ci, &previous_ci, &legacy] {
        let m = &package.metadata;
        let path = dir
            .path()
            .join("packages")
            .join(&m.product)
            .join(&m.version)
            .join(&m.platform)
            .join(&m.file_name);
        assert!(!path.exists());
        assert!(matches!(
            store.get_package(&package.id).await,
            Err(StoreError::NotFound(_))
        ));
    }
    for package in [
        current_ci,
        previous_stable,
        recent_ci,
        unknown,
        stable,
        beta,
    ] {
        assert!(store.get_package(&package.id).await.is_ok());
    }
    assert_eq!(store.cleanup_settings().await.unwrap(), None);
    let reupload = store
        .create_upload(unpublished_ci.metadata.clone())
        .await
        .unwrap();
    assert_ne!(reupload.id, unpublished_ci.id);
}

#[tokio::test]
async fn seven_day_cleanup_includes_old_upper_ci_and_preserves_existing_stable() {
    let dir = tempfile::tempdir().unwrap();
    let store = SoftwareStore::new(dir.path().into(), 1024).unwrap();
    let upper_ci = |version: &str| PackageMetadata {
        product: "mskdsp-upper".into(),
        version: version.into(),
        platform: "windows-x86_64-nsis".into(),
        file_name: "installer.exe".into(),
        channel: "ci".into(),
        source_ref: "main".into(),
        source_sha: format!("ffc9f0b{}", "0".repeat(33)),
        signature: "test-signature".into(),
        ..metadata(CONTENT)
    };
    let old = upload(
        &store,
        upper_ci("0.8.0-ci.20261030t040804+sha.ffc9f0b"),
        CONTENT,
    )
    .await;
    let stable = upload(
        &store,
        upper_ci("0.8.0-ci.20261030t034035+sha.ffc9f0b"),
        CONTENT,
    )
    .await;
    age(&store, &old).await;
    age(&store, &stable).await;
    store.publish(&old.id, "ci").await.unwrap();
    store.withdraw(&old.id, "ci").await.unwrap();
    store.publish(&stable.id, "stable").await.unwrap();
    // 模拟旧版本只保留曾发布标记与当前通道引用。
    let old_id = old.id.clone();
    store
        .run(move |inner| {
            inner
                .db
                .execute("DELETE FROM package_publications", [])
                .map_err(storage)?;
            inner
                .db
                .execute(
                    "INSERT INTO package_publications VALUES(?1,'unknown')",
                    [old_id],
                )
                .map_err(storage)?;
            Ok(())
        })
        .await
        .unwrap();
    drop(store);

    let store = SoftwareStore::new(dir.path().into(), 1024).unwrap();
    store.withdraw(&stable.id, "stable").await.unwrap();
    let preview = store.preview_cleanup(7).await.unwrap();
    let ids: Vec<_> = preview.packages.into_iter().map(|p| p.id).collect();
    assert_eq!(ids, [old.id.clone()]);
    let result = store
        .set_cleanup_days(Some(7))
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert_eq!(result.deleted, 1);
    assert!(store.get_package(&stable.id).await.is_ok());
}

#[tokio::test]
async fn enabling_cleanup_runs_now_and_setting_survives_reopen() {
    let dir = tempfile::tempdir().unwrap();
    let store = SoftwareStore::new(dir.path().into(), 1024).unwrap();
    let first = upload(&store, lower_ci(1), CONTENT).await;
    age(&store, &first).await;
    assert!(store.cleanup_scheduled().await.unwrap().is_none());
    assert!(store.get_package(&first.id).await.is_ok());

    let result = store
        .set_cleanup_days(Some(7))
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert_eq!(result.deleted, 1);
    assert_eq!(store.cleanup_settings().await.unwrap(), Some(7));
    drop(store);

    let store = SoftwareStore::new(dir.path().into(), 1024).unwrap();
    assert_eq!(store.cleanup_settings().await.unwrap(), Some(7));
    let result = store
        .set_cleanup_days(Some(60))
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert_eq!(result.deleted, 0);
    assert_eq!(store.cleanup_settings().await.unwrap(), Some(60));
    let second = upload(&store, lower_ci(2), CONTENT).await;
    age(&store, &second).await;
    assert_eq!(store.cleanup_scheduled().await.unwrap().unwrap().deleted, 1);
    assert!(store.set_cleanup_days(None).await.unwrap().is_none());
    assert!(matches!(
        store.set_cleanup_days(Some(8)).await,
        Err(StoreError::Invalid(_))
    ));
    drop(store);

    let store = SoftwareStore::new(dir.path().into(), 1024).unwrap();
    assert_eq!(store.cleanup_settings().await.unwrap(), None);
    let third = upload(&store, lower_ci(3), CONTENT).await;
    age(&store, &third).await;
    assert!(store.cleanup_scheduled().await.unwrap().is_none());
    assert!(store.get_package(&third.id).await.is_ok());
}

#[tokio::test]
async fn cleanup_rechecks_preview_after_channel_recommendation_changes() {
    let dir = tempfile::tempdir().unwrap();
    let store = SoftwareStore::new(dir.path().into(), 1024).unwrap();
    let package = upload(&store, lower_ci(1), CONTENT).await;
    age(&store, &package).await;
    let preview = candidates(&store).await;
    assert_eq!(preview, BTreeSet::from([package.id.clone()]));

    store.publish(&package.id, "ci").await.unwrap();
    assert!(matches!(
        store.cleanup_once(30, preview.into_iter().collect()).await,
        Err(StoreError::Conflict(_))
    ));
    assert_eq!(
        store.get_package(&package.id).await.unwrap().channels,
        ["ci"]
    );
    assert!(
        store
            .download_file(&package.id, &package.metadata.file_name)
            .await
            .is_ok()
    );
}

#[tokio::test]
async fn scheduled_maintenance_finishes_committed_deletions_when_disabled() {
    let dir = tempfile::tempdir().unwrap();
    let store = SoftwareStore::new(dir.path().into(), 1024).unwrap();
    let package = upload(&store, lower_ci(1), CONTENT).await;
    let id = package.id.clone();
    let path = store
        .run(move |inner| {
            let tx = inner.db.transaction().map_err(storage)?;
            tx.execute(
                "INSERT INTO pending_package_deletions
                 SELECT id,product,version,platform,metadata FROM packages WHERE id=?1",
                [&id],
            )
            .map_err(storage)?;
            tx.execute("DELETE FROM uploads WHERE package_id=?1", [&id])
                .map_err(storage)?;
            tx.execute("DELETE FROM packages WHERE id=?1", [&id])
                .map_err(storage)?;
            tx.commit().map_err(storage)?;
            inner.package_path(&package.metadata)
        })
        .await
        .unwrap();
    assert!(path.exists());
    drop(store);

    let store = SoftwareStore::new(dir.path().into(), 1024).unwrap();
    assert_eq!(store.cleanup_settings().await.unwrap(), None);
    let result = store.cleanup_scheduled().await.unwrap().unwrap();
    assert_eq!(result.deleted, 0);
    assert_eq!(result.freed_bytes, CONTENT.len() as u64);
    assert_eq!(result.pending, 0);
    assert!(!path.exists());
    assert!(store.cleanup_scheduled().await.unwrap().is_none());
}

#[cfg(unix)]
#[tokio::test]
async fn cleanup_preserves_an_open_download() {
    let dir = tempfile::tempdir().unwrap();
    let store = SoftwareStore::new(dir.path().into(), 1024).unwrap();
    let package = upload(&store, lower_ci(1), CONTENT).await;
    age(&store, &package).await;
    store.publish(&package.id, "ci").await.unwrap();
    store.withdraw(&package.id, "ci").await.unwrap();
    let (_, mut open_file) = store
        .download_file(&package.id, &package.metadata.file_name)
        .await
        .unwrap();

    let result = store
        .cleanup_once(7, vec![package.id.clone()])
        .await
        .unwrap();
    assert_eq!(result.deleted, 1);
    assert!(matches!(
        store
            .download_file(&package.id, &package.metadata.file_name)
            .await,
        Err(StoreError::NotFound(_))
    ));
    let mut data = Vec::new();
    open_file.read_to_end(&mut data).unwrap();
    assert_eq!(data, CONTENT);
}

#[cfg(unix)]
#[tokio::test]
async fn cleanup_preserves_platform_current_previous_and_latest() {
    let dir = tempfile::tempdir().unwrap();
    let runtime = dir.path().join("runtime");
    fs::create_dir(&runtime).unwrap();
    let store = SoftwareStore::new(dir.path().join("software"), 1024)
        .unwrap()
        .with_runtime_directory(Some(runtime.clone()));

    let mut packages = Vec::new();
    for job in [98, 99, 100, 101] {
        let mut m = tests::platform_metadata(CONTENT);
        m.version = format!("0.0.1-ci.20261018t094236.6.{job}+sha.aaaaaaa");
        let package = upload(&store, m, CONTENT).await;
        age(&store, &package).await;
        packages.push(package);
    }
    let [old, previous, current, latest] = packages.as_slice() else {
        unreachable!();
    };
    let mut tied_metadata = latest.metadata.clone();
    tied_metadata.version = "0.0.1-ci.20261018t094236.6.101+sha.bbbbbbb".into();
    tied_metadata.source_sha = "b".repeat(40);
    let tied_latest = upload(&store, tied_metadata, CONTENT).await;
    age(&store, &tied_latest).await;
    let runtime_identity = |p: &Package| {
        serde_json::json!({
            "version": p.metadata.version,
            "channel": p.metadata.channel,
            "commit": p.metadata.source_sha,
            "build_time": p.metadata.build_time,
            "release": p.metadata.version.replace('+', "_"),
        })
    };
    let mut state = runtime_identity(current);
    state["previous"] = runtime_identity(previous);
    fs::write(runtime.join("state.json"), state.to_string()).unwrap();

    let eligible = candidates(&store).await;
    assert_eq!(eligible, BTreeSet::from([old.id.clone()]));
    let updater_lock = fs::File::open(runtime.join(".update.lock")).unwrap();
    rustix::fs::flock(&updater_lock, rustix::fs::FlockOperation::LockExclusive).unwrap();
    assert!(candidates(&store).await.is_empty());
    drop(updater_lock);
    let result = store
        .cleanup_once(30, eligible.into_iter().collect())
        .await
        .unwrap();
    assert_eq!(result.deleted, 1);
    assert!(matches!(
        store.get_package(&old.id).await,
        Err(StoreError::NotFound(_))
    ));
    for package in [previous, current, latest, &tied_latest] {
        assert!(store.get_package(&package.id).await.is_ok());
    }
}
