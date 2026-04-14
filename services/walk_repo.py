import os 


def keep_only_code_files(dirpath,dirnames,filenames):
    dirnames[:] = [d for d in dirnames if d not in ["__pycache__", ".git"]]
    filenames[:] = [f for f in filenames if f.endswith((".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".scss", ".json", ".yaml", ".yml", ".md", ".txt", ".csv", ".xls", ".xlsx", ".ppt", ".pptx"))]
    return dirpath,dirnames,filenames

async def walk_repo(repo_dir:str):
    file_tree = ""
    for dirpath, dirnames, filenames in os.walk(repo_dir):
        dirpath,dirnames,filenames = keep_only_code_files(dirpath,dirnames,filenames)
        level = dirpath.replace(repo_dir,"").count(os.sep)
        indent = "  "*level
        file_tree += f"{indent}{os.path.basename(dirpath)}/\n"
        for f in filenames:
            file_tree += f"{indent}{f}\n"
    return file_tree.strip()
