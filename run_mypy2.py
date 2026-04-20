import subprocess
import sys

result = subprocess.run(
    [
        sys.executable, "-m", "mypy",
        r"C:\Users\deyzh\Projects\axon\src\axon\providers\aws.py",
        r"C:\Users\deyzh\Projects\axon\src\axon\providers\gcp.py",
        r"C:\Users\deyzh\Projects\axon\src\axon\providers\azure.py",
        "--config-file", r"C:\Users\deyzh\Projects\axon\pyproject.toml",
        "--show-error-codes",
        "--verbose",
    ],
    capture_output=True,
    text=True,
)

output = result.stdout + result.stderr
with open(r"C:\Users\deyzh\Projects\axon\mypy_result2.txt", "w") as f:
    f.write(output)
    f.write(f"\nEXIT CODE: {result.returncode}\n")

print(output[-3000:] if len(output) > 3000 else output)
print(f"EXIT CODE: {result.returncode}")
