//! 运行期设备连接及一次性隧道票据；所有数据会话随控制连接结束。
use super::proto::{PlatformMessage, TunnelFrame};
use std::{collections::HashMap, sync::Arc, time::Instant};
use tokio::sync::{Mutex, mpsc, oneshot};
use tokio_util::sync::CancellationToken;
use tonic::{Status, Streaming};

pub type FrameSender = mpsc::Sender<Result<TunnelFrame, Status>>;
pub struct Peer {
    pub inbound: Streaming<TunnelFrame>,
    pub outbound: FrameSender,
    pub token: String,
}
#[derive(Clone)]
pub struct LiveDevice {
    pub connection_id: String,
    pub services: Vec<String>,
    pub agent_version: String,
    pub outbound: mpsc::Sender<Result<PlatformMessage, Status>>,
    pub cancel: CancellationToken,
}
pub struct Pending {
    pub device_id: String,
    pub connection_id: String,
    pub ticket: String,
    pub created: Instant,
    pub attach: Option<oneshot::Sender<Result<Peer, Status>>>,
}
#[derive(Default)]
pub struct Registry {
    pub devices: HashMap<String, LiveDevice>,
    pub tunnels: HashMap<String, Pending>,
}
#[derive(Clone, Default)]
pub struct Hub(pub Arc<Mutex<Registry>>);
impl Hub {
    pub async fn snapshot(&self, id: &str) -> Option<LiveDevice> {
        self.0.lock().await.devices.get(id).cloned()
    }
    pub async fn disconnect(&self, id: &str) {
        let live = self.0.lock().await.devices.remove(id);
        if let Some(live) = live {
            live.cancel.cancel();
        }
    }
    pub async fn remove_device(&self, id: &str, connection: &str) {
        let mut registry = self.0.lock().await;
        if registry
            .devices
            .get(id)
            .is_some_and(|d| d.connection_id == connection)
        {
            if let Some(live) = registry.devices.remove(id) {
                live.cancel.cancel();
            }
        }
    }
    pub async fn failure(&self, id: &str, connection: &str, tunnel: &str) {
        let mut registry = self.0.lock().await;
        if let Some(pending) = registry.tunnels.get_mut(tunnel) {
            if pending.device_id == id && pending.connection_id == connection {
                if let Some(sender) = pending.attach.take() {
                    let _ = sender.send(Err(Status::unavailable("下位机无法连接指定本机服务")));
                }
            }
        }
    }
}
