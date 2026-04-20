import subprocess
import sys

result = subprocess.run(
    [
        sys.executable, "-m", "mypy",
        r"C:\Users\deyzh\Projects\axon\src\axon\providers\aws.py",
        r"C:\Users\deyzh\Projects\axon\src\axon\providers\gcp.py",
        r"C:\Users\deyzh\Projects\axon\src\axon\providers\azure.py",
        "--config-file", r"C:\Users\deyzh\Projects\axon\pyproject.toml",
        "--no-error-summary",
        "--show-error-codes",
    ],
    capture_output=True,
    text=True,
)

output = result.stdout + result.stderr
with open(r"C:\Users\deyzh\Projects\axon\mypy_result.txt", "w") as f:
    f.write(output)
    f.write(f"\nEXIT CODE: {result.returncode}\n")

print(output)
print(f"EXIT CODE: {result.returncode}")
