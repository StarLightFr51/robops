import json 
from pathlib import Path
import generator

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "robops.config.json"
OUTPUT_FILE = ROOT / ".github" / "workflows" / "deploy.yml"
METADATA_FILE = ROOT / "robops.metadata.json"

METADATA = {
  "robops_version": 1.0, #need to request from version on github
}

def main():
    if not Path.exists(CONFIG_FILE):
      raise FileNotFoundError("""robops.config.json not in specified directory. 
      To generate your robops file please add this file and add inside it the required values""")

    if not Path.exists(METADATA_FILE):
      METADATA_FILE.parent.mkdir(parents=True, exist_ok=True)
      METADATA_FILE.write_text(json.dumps(METADATA))

    with METADATA_FILE.open("r", encoding="utf-8") as file:
      metadata = json.load(file)

      if METADATA["robops_version"] != metadata["robops_version"] :
        print("Careful your version is not updated, please install the newest version if you want, new features and get new security updates")

    with CONFIG_FILE.open("r", encoding="utf-8") as file: 
        config = json.load(file)

    required = {
        "workflow_name",
        "branch",
        "build_path",
        "environnement",
    }

    missing = required - config.keys()
    if missing:
        raise ValueError(f"Missing configuration keys: {sorted(missing)}")

    workflow = generator.generate_workflow(config)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(workflow, encoding="utf-8")

    print(f"Workflow generated: {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
