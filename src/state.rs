use std::{collections::HashMap, sync::Arc};
use tokio::sync::RwLock;

#[derive(Clone, Default)]
pub struct SessionRegistry {
    sessions: Arc<RwLock<HashMap<String, AgentSession>>>,
}

#[derive(Debug, Clone)]
pub struct AgentSession {
    pub connection_id: String,
    pub agent_id: String,
    pub site_id: String,
    pub agent_version: String,
}

impl SessionRegistry {
    pub async fn register(&self, session: AgentSession) {
        self.sessions
            .write()
            .await
            .insert(session.agent_id.clone(), session);
    }

    pub async fn remove(&self, agent_id: &str, connection_id: &str) {
        let mut sessions = self.sessions.write().await;
        if sessions
            .get(agent_id)
            .is_some_and(|session| session.connection_id == connection_id)
        {
            sessions.remove(agent_id);
        }
    }

    pub async fn count(&self) -> usize {
        self.sessions.read().await.len()
    }
}
