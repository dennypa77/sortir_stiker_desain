import re

def increment_version():
    with open('app.py', 'r', encoding='utf-8') as file:
        content = file.read()

    # Regex untuk mencari string title dengan versi, misal: self.title("Bot Sortir Stiker & Gudang v2.0")
    pattern = r'(self\.title\("Bot Sortir Stiker & Gudang v)(\d+)\.(\d+)("\))'
    
    def repl(match):
        major = int(match.group(2))
        minor = int(match.group(3))
        # Naikkan minor version
        new_minor = minor + 1
        new_version = f"{major}.{new_minor}"
        print(f"Versi dinaikkan secara otomatis dari {major}.{minor} menjadi {new_version}")
        return f"{match.group(1)}{new_version}{match.group(4)}"
        
    new_content, count = re.subn(pattern, repl, content)
    
    if count > 0:
        with open('app.py', 'w', encoding='utf-8') as file:
            file.write(new_content)
    else:
        print("Tidak menemukan format versi (vX.X) di app.py untuk dinaikkan.")

if __name__ == '__main__':
    increment_version()
