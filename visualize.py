from graph import app

print("=== Inbox Agent Graph (ASCII) ===\n")
print(app.get_graph().draw_ascii())

print("\n=== Mermaid Diagram ===\n")
print(app.get_graph().draw_mermaid())
