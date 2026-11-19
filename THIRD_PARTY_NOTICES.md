# Third-party notices

## rbxcache

Patchwork's reading and writing of Roblox's asset cache (`patchwork/cache.py`) is ported from rbxcache: the `RBXH` entry format, the checksum and how its key is derived, and the procedure for replacing an entry's body.

Source: https://github.com/PirxcyFinal/rbxcache

```
MIT License

Copyright (c) 2026 pirxcy

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

rbxcache refuses to swap 3D content. Patchwork allows like-for-like swaps of meshes and models (skins) but, like rbxcache, never removes or blanks 3D objects and never touches collision data.
