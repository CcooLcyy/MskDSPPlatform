use serde::Serialize;

#[derive(Clone, Copy, Serialize)]
pub struct BuildInfo {
    pub service: &'static str,
    pub version: &'static str,
    pub base_version: &'static str,
    pub channel: &'static str,
    pub commit: &'static str,
    pub build_time: &'static str,
}

impl BuildInfo {
    pub fn current() -> Self {
        Self {
            service: "mskdsp-platform",
            version: option_env!("MSKDSP_BUILD_VERSION").unwrap_or(env!("CARGO_PKG_VERSION")),
            base_version: env!("CARGO_PKG_VERSION"),
            channel: option_env!("MSKDSP_BUILD_CHANNEL").unwrap_or("dev"),
            commit: option_env!("MSKDSP_BUILD_COMMIT").unwrap_or("unknown"),
            build_time: option_env!("MSKDSP_BUILD_TIME").unwrap_or("unknown"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // 更新器读取的命令行身份必须与 HTTP 版本接口使用同一套编译元数据。
    #[test]
    fn build_identity_is_complete_json() {
        let data = serde_json::to_value(BuildInfo::current()).unwrap();
        assert_eq!(data["service"], "mskdsp-platform");
        assert_eq!(data["base_version"], env!("CARGO_PKG_VERSION"));
        for field in ["version", "channel", "commit", "build_time"] {
            assert!(data[field].as_str().is_some_and(|value| !value.is_empty()));
        }
    }
}
