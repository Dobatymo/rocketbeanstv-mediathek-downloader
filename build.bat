py -m pip install -r requirements.txt --target rbtv-mediathek
del /F /Q "rbtv-mediathek\bin"
py -m zipapp rbtv-mediathek -c
