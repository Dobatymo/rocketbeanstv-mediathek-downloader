mkdir "rbtv-mediathek"
py -m pip install -r requirements.txt --target rbtv-mediathek
copy rbtv-mediathek.py rbtv-mediathek\__main__.py
del /F /Q "rbtv-mediathek\bin"
py -m zipapp rbtv-mediathek -c
del /Q /S "rbtv-mediathek" > NUL
rmdir /Q /S "rbtv-mediathek" > NUL
