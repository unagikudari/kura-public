"""Failed rendering must never publish a fake black poster or destroy a real one."""
import errno
from pathlib import Path
import subprocess
from PIL import Image
import pytest
from api.routers import upload as U


def test_invalid_image_leaves_no_synthetic_poster(tmp_path):
    src=tmp_path/'original';src.write_bytes(b'invalid image');dest=tmp_path/'poster.jpg'
    assert U._generate_thumbnail(src,dest,'image/jpeg') is False
    assert not dest.exists() and src.read_bytes()==b'invalid image'
    assert not list(tmp_path.glob('.thumbnail-*'))


def test_failed_pdf_keeps_an_existing_good_poster(tmp_path,monkeypatch):
    src=tmp_path/'original.pdf';src.write_bytes(b'PDF input');dest=tmp_path/'poster.jpg'
    Image.new('RGB',(30,20),'red').save(dest);before=dest.read_bytes()
    def fail(command,**kwargs):
        assert '-scale-to' in command
        raise subprocess.CalledProcessError(1,command)
    monkeypatch.setattr(U.subprocess,'run',fail)
    assert U._generate_thumbnail(src,dest,'application/pdf') is False
    assert dest.read_bytes()==before and src.read_bytes()==b'PDF input'


def test_partial_output_is_not_published_and_disk_errors_propagate(tmp_path,monkeypatch):
    dest=tmp_path/'poster.jpg';dest.write_bytes(b'previous complete poster')
    def partial(src,out,mime):
        out.write_bytes(b'partial');raise ValueError('decoder failed')
    monkeypatch.setattr(U,'_render_thumbnail',partial)
    assert U._generate_thumbnail(tmp_path/'source',dest,'image/png') is False
    assert dest.read_bytes()==b'previous complete poster'
    def full(src,out,mime):raise OSError(errno.ENOSPC,'full')
    monkeypatch.setattr(U,'_render_thumbnail',full)
    with pytest.raises(OSError) as error:U._generate_thumbnail(tmp_path/'source',dest,'image/png')
    assert error.value.errno==errno.ENOSPC and dest.read_bytes()==b'previous complete poster'


def test_real_jpeg_is_bounded_and_original_is_unchanged(tmp_path):
    src=tmp_path/'original.jpg';Image.new('RGB',(4000,3000),'green').save(src);before=src.read_bytes()
    dest=tmp_path/'poster.jpg'
    assert U._generate_thumbnail(src,dest,'image/jpeg') is True
    with Image.open(dest) as image:
        image.load();assert image.width>1 and image.height>1
        assert image.width<=U.THUMBNAIL_SIZE[0] and image.height<=U.THUMBNAIL_SIZE[1]
    assert src.read_bytes()==before and not list(tmp_path.glob('.thumbnail-*'))
