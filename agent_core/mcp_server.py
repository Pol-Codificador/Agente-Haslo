"""Servidor MCP (Model Context Protocol) que expone las herramientas de Vortex AI Desktop."""

from __future__ import annotations

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # Compatibilidad con mcp < 2.0
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[no-redef]

from agent_core.tools import (
    activate_workspace_mode,
    analyze_pdf_with_gemini,
    ask_web_ai_in_brave,
    browse_and_capture,
    build_with_heavy_web_ai,
    control_app_window,
    create_calendar_event,
    create_google_doc_in_brave,
    deep_search_in_files,
    download_classroom_attachment,
    get_calendar_schedule,
    get_classroom_pending,
    get_dutic_events,
    get_system_status,
    git_project_manager,
    gui_click_and_type,
    list_recent_downloads,
    media_control,
    open_and_automate_brave_service,
    open_application_or_url,
    organize_or_inspect_folder,
    pc_clipboard_and_voice,
    read_local_pdf_or_file,
    run_claude_code_task,
    run_terminal_command,
    save_to_second_brain,
    search_second_brain,
    send_file_to_whatsapp,
    send_whatsapp_to_contact,
    set_pc_or_task_alert,
    summarize_youtube_video,
    take_screenshot,
    verify_file,
    wait_for_new_download,
    webcam_security_snapshot,
)

mcp = MCPServer("Vortex-AI-Desktop")

for tool_fn in (
    get_dutic_events,
    get_classroom_pending,
    get_calendar_schedule,
    create_calendar_event,
    take_screenshot,
    get_system_status,
    run_terminal_command,
    open_application_or_url,
    media_control,
    browse_and_capture,
    gui_click_and_type,
    create_google_doc_in_brave,
    ask_web_ai_in_brave,
    control_app_window,
    send_file_to_whatsapp,
    read_local_pdf_or_file,
    run_claude_code_task,
    save_to_second_brain,
    search_second_brain,
    summarize_youtube_video,
    set_pc_or_task_alert,
    organize_or_inspect_folder,
    pc_clipboard_and_voice,
    build_with_heavy_web_ai,
    send_whatsapp_to_contact,
    open_and_automate_brave_service,
    activate_workspace_mode,
    webcam_security_snapshot,
    git_project_manager,
    deep_search_in_files,
    download_classroom_attachment,
    analyze_pdf_with_gemini,
    list_recent_downloads,
    wait_for_new_download,
    verify_file,
):
    mcp.tool()(tool_fn)


if __name__ == "__main__":
    mcp.run()
