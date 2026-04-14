import subprocess

async def clone_repo(repo_url:str, dest_dir:str):
    try:
        subprocess.run(["git","clone", repo_url, dest_dir], check=True)
        return True
    except subprocess.CalledProcessError as e:
        return False
    except Exception as e:
        return False