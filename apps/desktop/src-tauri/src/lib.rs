#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![agent_decide])
        .run(tauri::generate_context!())
        .expect("failed to start Heli bot");
}

#[tauri::command]
fn agent_decide(
    tool: String,
    path: Option<String>,
    app: Option<String>,
    run_command: bool,
    confirm_writes: bool,
    allowed_paths: Vec<String>,
    allowed_apps: Vec<String>,
    stopped: bool,
) -> String {
    let policy = local_agent::Policy {
        run_command,
        confirm_writes,
        allowed_paths,
        allowed_apps,
        stopped,
    };
    let action = local_agent::Action::new(tool, path, app);
    match local_agent::decide(&policy, &action) {
        local_agent::Decision::Allow => "allow",
        local_agent::Decision::Confirm => "confirm",
        local_agent::Decision::Deny => "deny",
    }
    .to_string()
}
