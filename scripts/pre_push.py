import os
import sys
import subprocess

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(script_dir, ".."))
    os.chdir(project_root)
    
    print("==================================================")
    print("            MeshHub Pre-push Validator            ")
    print("==================================================")
    
    # 1. Validate configuration file
    print("[Pre-push] Validating config.json schema...")
    res = subprocess.run([sys.executable, "scripts/validate_config.py"])
    if res.returncode != 0:
        print("[Pre-push Error] Configuration validation failed. Push aborted.")
        sys.exit(1)
    print("[Pre-push] Configuration validated successfully.")
    
    # 2. Run unit tests
    print("\n[Pre-push] Running framework unit tests...")
    res = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "*.py"])
    if res.returncode != 0:
        print("[Pre-push Error] Framework unit tests failed. Push aborted.")
        sys.exit(1)
    print("[Pre-push] Framework unit tests passed.")
    
    print("\n==================================================")
    print("Pre-push checks PASSED. Proceeding with Git push.")
    print("==================================================")
    sys.exit(0)

if __name__ == '__main__':
    main()
