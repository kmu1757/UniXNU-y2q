#!/usr/bin/env python3
"""Build SM8250/SM8475 GMU/CP/native-BO support and its pong_adreno_start=1 entry.
Hardware startup is attempted only by the kernel boot-arg entry. The native
client is published only after real CP init and authenticated nonsecure fences.
This compiler does not access a phone or run image verification.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import struct
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import pong_paths


def patch_pong_adreno_for_t970(adreno_source: Path) -> None:
    try:
        import patch_pong_adreno_t970
    except ImportError:
        for p in (Path(__file__).resolve().parent, ROOT/'scripts', Path(__file__).resolve().parents[3]/'scripts'):
            if p.exists() and str(p) not in sys.path:
                sys.path.insert(0, str(p))
        import patch_pong_adreno_t970
    patch_pong_adreno_t970.patch_pong_adreno(adreno_source)


def stage_late_initializer(obj_path: Path, new_name: bytes, env: dict) -> Path:
    symbols=subprocess.check_output(['xcrun','nm','-j',str(obj_path)],env=env,text=True).splitlines()
    initializer=next(s for s in symbols if s.startswith('__GLOBAL__sub_I_'))
    # LLVM18 cannot globalize Mach-O symbols. Change only this owned object's
    # initializer registration and symbol linkage; keep all code/relocations.
    raw=bytearray(obj_path.read_bytes());offset=32;symbol_command=None
    for _ in range(struct.unpack_from('<I',raw,16)[0]):
        command,length=struct.unpack_from('<II',raw,offset)
        if command==0x19:
            sections=struct.unpack_from('<I',raw,offset+64)[0]
            for index in range(sections):
                section=offset+72+80*index
                if raw[section:section+16].rstrip(b'\0')==b'__mod_init_func':
                    raw[section:section+16]=b'__pong_init'.ljust(16,b'\0')
                    flags=struct.unpack_from('<I',raw,section+64)[0]
                    struct.pack_into('<I',raw,section+64,flags&~255)
        elif command==2:symbol_command=offset
        offset+=length
    if symbol_command is None:raise RuntimeError('Owned object has no symbol table: '+str(obj_path))
    symoff,count,stroff,strsize=struct.unpack_from('<4I',raw,symbol_command+8)
    if stroff+strsize!=len(raw):raise RuntimeError('Unexpected owned-object string table layout: '+str(obj_path))
    for index in range(count):
        at=symoff+16*index;name_at=stroff+struct.unpack_from('<I',raw,at)[0]
        end=raw.index(0,name_at)
        if raw[name_at:end].decode()==initializer:
            struct.pack_into('<I',raw,at,strsize);raw[at+4]|=1
            raw.extend(new_name)
            struct.pack_into('<I',raw,symbol_command+20,strsize+len(new_name));break
    else:raise RuntimeError('Owned initializer not found: '+str(obj_path))
    late=obj_path.with_suffix('.late.o');late.write_bytes(raw)
    return late


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--reference-dir',type=Path,
        help='regenerate hardware facts from the matching vendor gen7_reg.h')
    p.add_argument('--output-dir',required=True,type=Path)
    p.add_argument('--guest-build',choices=('20G81','22H374'),default='20G81')
    p.add_argument('--tree',type=Path,default=pong_paths.build_tree(name='Pong-iOS16-v45-high-scratch'))
    p.add_argument('--firmware-dir',type=Path,
        help='local REC-extracted Adreno firmware; defaults to PONG_ADRENO_FIRMWARE or guest output')
    p.add_argument('--developer-dir',type=Path,default=pong_paths.developer_dir())
    a=p.parse_args();out=a.output_dir.resolve();out.mkdir(parents=True,exist_ok=True)
    if a.firmware_dir is None:
        # PONG_ADRENO_FIRMWARE wins. Otherwise the guest's existing output path.
        # build-xnu-ipados18.sh does not pass --firmware-dir.
        found=pong_paths.adreno_firmware()
        a.firmware_dir=found if found is not None else ROOT/('out/ipados18' if a.guest_build=='22H374' else 'out')/'firmware/adreno730'
    a650_files=[('PongAdrenoGmuFirmware','a650_gmu.bin'),
        ('PongAdrenoSqeFirmware','a650_sqe.fw'),('PongAdrenoZapMdt','a650_zap.mdt'),
        ('PongAdrenoZapCode','a650_zap.b02'),('PongAdrenoZapSignature','a650_zap.b01')]
    a730_files=[('PongAdrenoGmuFirmware','gmu_gen70000.bin'),
        ('PongAdrenoSqeFirmware','a730_sqe.fw'),('PongAdrenoZapMdt','a730_zap.mdt'),
        ('PongAdrenoZapCode','a730_zap.b01'),('PongAdrenoZapSignature','a730_zap.b02')]
    if all((a.firmware_dir/f).is_file() for _,f in a650_files):
        fw_files=a650_files;fw_present=True;gmu_fw_name='a650_gmu.bin';sqe_fw_name='a650_sqe.fw';zap_name='a650_zap'
    elif all((a.firmware_dir/f).is_file() for _,f in a730_files):
        fw_files=a730_files;fw_present=True;gmu_fw_name='gmu_gen70000.bin';sqe_fw_name='a730_sqe.fw';zap_name='a730_zap'
    else:
        fw_files=[(name,None) for name,_ in a650_files];fw_present=False
        gmu_fw_name='placeholder(0B)';sqe_fw_name='placeholder(0B)';zap_name='placeholder(0B)'
    source=ROOT/'drivers/PongAdreno'
    patch_pong_adreno_for_t970(source)
    target='arm64-apple-ios18.0' if a.guest_build=='22H374' else 'arm64-apple-ios16.0'
    regs=(a.reference_dir/'gen7_reg.h').read_text() if a.reference_dir else (source/'PongAdrenoCpProfile.h').read_text()
    needed=sorted(set(re.findall(r'\bGEN7_[A-Z0-9_]+\b',(source/'PongAdrenoGmu.c').read_text())))
    values={}
    for name in needed:
        match=re.search(r'^#define\s+'+name+r'\s+(0x[0-9a-fA-F]+)[uU]?\s*$',regs,re.M)
        if not match:raise RuntimeError('Missing actual Pong register '+name)
        values[name]=int(match[1],16)
    header='/* Register facts from NothingOSS SM8475 gen7_reg.h. Dword offsets. */\n'
    header+='\n'.join('#define '+name+' '+hex(value)+'u' for name,value in values.items())+'\n'
    (out/'PongAdrenoRegisters.h').write_text(header)
    env=dict(os.environ,DEVELOPER_DIR=str(a.developer_dir))
    sdk=subprocess.check_output(['xcrun','--sdk','iphoneos','--show-sdk-path'],env=env,text=True).strip()
    commands=[];objects=[]
    sources=['AdrenoFirmware','PongAdrenoHfi','PongAdrenoGmu','PongAdrenoSmmu',
             'PongAdrenoPower','PongAdrenoClocks','PongAdrenoZap']
    sources+=['submit/AdrenoPM4']
    # Darwin24 links one helper archive for GPU/Wi-Fi/audio. Preserve the
    # standalone iOS16 recipe, whose linker has no shared archive entry.
    if a.guest_build != '22H374':
        sources+=['ArmSmmu4K/'+name for name in ['ArmSmmu4K','ArmSmmu4KContext',
            'ArmSmmu4KProgram','ArmSmmu4KStreams','ArmSmmu4KAudit',
            'ArmSmmu4KLive','ArmSmmu4KTableMemory','ArmSmmu4KMmio']]
    for name in sources:
        obj=out/(Path(name).name+'.o')
        cmd=['xcrun','clang','-target',target,'-isysroot',sdk,
             '-mkernel','-ffreestanding','-fno-builtin','-fno-stack-protector','-mgeneral-regs-only',
             '-fno-asynchronous-unwind-tables','-Os',
             '-Wall','-Wextra','-Werror','-I'+str(out),'-I'+str(source),
             '-I'+str(source/'ArmSmmu4K'),
             '-c',str(source/(name+'.c')),'-o',str(obj)]
        subprocess.run(cmd,env=env,check=True);commands.append(cmd);objects.append(obj)
    # In public CI where private firmware is absent, emit 0-byte placeholders
    # (PongAdrenoGmuStart/PlanZap/CpStart reject 0-byte blobs before any MMIO).
    data=out/'PongAdrenoFirmware.generated.c'
    embedded='#include <stddef.h>\n#include <stdint.h>\n'
    for name,file in fw_files:
        if fw_present and file is not None:
            firmware=(a.firmware_dir/file).read_bytes()
            embedded+='const uint8_t '+name+'[]={\n'+''.join(
                ','.join(str(x) for x in firmware[i:i+32])+',\n' for i in range(0,len(firmware),32))
            embedded+='};\nconst size_t '+name+'Bytes=sizeof('+name+');\n'
        else:
            embedded+='const uint8_t '+name+'[]={0};\nconst size_t '+name+'Bytes=0u;\n'
    data.write_text(embedded)
    obj=out/'PongAdrenoFirmware.generated.o'
    cmd=commands[0].copy();cmd[cmd.index('-c')+1]=str(data);cmd[-1]=str(obj)
    subprocess.run(cmd,env=env,check=True);commands.append(cmd);objects.append(obj)
    framework=a.tree/'.cache/fakeroot/System/Library/Frameworks/Kernel.framework/Versions/A'
    obj=out/'PongAdrenoPlatform.o'
    cmd=['xcrun','clang++','-target',target,'-isysroot',sdk,
        '-mkernel','-ffreestanding','-fno-builtin','-fno-stack-protector','-mgeneral-regs-only',
        '-fno-exceptions','-fno-rtti','-fno-stack-check','-std=gnu++17','-fblocks','-Os',
        '-DKERNEL=1','-DKERNEL_PRIVATE=1','-DXNU_KERNEL_PRIVATE=1','-DARM64_BOARD_CONFIG_PONG=1',
        *(['-DPONG_GUEST_22H374=1'] if a.guest_build=='22H374' else []),
        '-I'+str(framework/'PrivateHeaders'),'-I'+str(framework/'Headers'),
        '-I'+str(out),'-I'+str(source),'-I'+str(source/'ArmSmmu4K'),
        '-Wno-deprecated-declarations','-Werror=uninitialized','-Werror=return-type',
        '-c',str(source/'PongAdrenoPlatform.cpp'),'-o',str(obj)]
    subprocess.run(cmd,env=env,check=True);commands.append(cmd);objects.append(obj)
    cpp_template=cmd.copy()
    names=['PongAdrenoCp','PongAdrenoNativeGlue','PongAdrenoScanout','PongAdrenoAccelerator']+['submit/'+n for n in [
        'NativeAdrenoBoard','NativeAdrenoClient','NativeAdrenoPublish','NativeAdrenoRing','NativeAdrenoCpInit','NativeAdrenoTimer','NativeAdrenoIrq','NativeAdrenoPwrup',
        'NativeAdrenoGpuContext','NativeAdrenoCpu','NativeAdrenoMemory','NativeAdrenoSmmuMemory',
        'NativeAdrenoSmmuContext','NativeAdrenoVm','NativeAdrenoSubmit','NativeAdrenoUserClient']]
    for name in names:
        obj=out/(Path(name).name+'.o');cmd=cpp_template.copy()
        cmd[cmd.index('-c')+1]=str(source/(name+'.cpp'));cmd[-1]=str(obj)
        cmd+=['-I'+str(source/'submit')]
        subprocess.run(cmd,env=env,check=True);commands.append(cmd);objects.append(obj)
    # Sheng's late accelerator class: bind the selected guest's IOAcceleratorES
    # ancestry, and defer its constructor until IOGPUFamily is loaded.
    abi=json.loads((ROOT/('configs/pong-iogpu-'+a.guest_build+'.json')).read_text())
    bindings_path=ROOT/'mac-build/native-ufs/bindings.json'
    fixed=json.loads(bindings_path.read_text()) if bindings_path.is_file() else {}
    bindings={'__ZN15IOAcceleratorESC2EPK11OSMetaClass':'constructor',
              '__ZN15IOAcceleratorESD2Ev':'destructor',
              '__ZN15IOAcceleratorES10gMetaClassE':'metaclass',
              '_PongIOAcceleratorMetaConstruct':'metaclass_constructor'}
    if 'fixed_prelink_base' in fixed and os.environ.get('PONG_STORAGE_BINDING','self')!='self':
        if fixed.get('build', '20G81') != a.guest_build:raise RuntimeError('GPU and storage prelink guests differ')
        slide=int(fixed['fixed_prelink_base'],16)-int(abi['original_prelink_base'],16)
        assembly=out/'PongIOAcceleratorBindings.S'
        assembly.write_text(''.join('.globl '+symbol+'\n.set '+symbol+', '+hex(int(abi[key],16)+slide)+'\n'
            for symbol,key in bindings.items()))
        binding_object=out/'PongIOAcceleratorBindings.o'
        cmd=['xcrun','clang','-target',target,'-isysroot',sdk,'-c',str(assembly),'-o',str(binding_object)]
        subprocess.run(cmd,env=env,check=True);commands.append(cmd);objects.append(binding_object)
        parent_bindings={s:hex(int(abi[k],16)+slide) for s,k in bindings.items()}
    else:
        bind_cpp=out/'PongIOAcceleratorBindings.cpp'
        bind_cpp.write_text('#include "PongIOAcceleratorABI.h"\nOSDefineMetaClassAndAbstractStructors(IOAcceleratorES,IOService)\n')
        raw_bind=out/'PongIOAcceleratorBindings.o';cmd=cpp_template.copy()
        cmd[cmd.index('-c')+1]=str(bind_cpp);cmd[-1]=str(raw_bind)
        subprocess.run(cmd,env=env,check=True);commands.append(cmd)
        objects.append(stage_late_initializer(raw_bind,b'_PongIOAcceleratorMetaConstruct\0',env))
        parent_bindings={s:'self-contained' for s in bindings}
    accelerator=out/'NativeAdrenoAccelerator.o';cmd=cpp_template.copy()
    cmd[cmd.index('-c')+1]=str(source/'submit/NativeAdrenoAccelerator.cpp');cmd[-1]=str(accelerator)
    cmd+=['-I'+str(source/'submit')]
    subprocess.run(cmd,env=env,check=True);commands.append(cmd)
    objects.append(stage_late_initializer(accelerator,b'_PongAdrenoAcceleratorStaticInit\0',env))
    archive=out/'libPongAdrenoPlatform.a'
    # ar rcs otherwise retains stale members from the replaced client wrapper.
    archive.unlink(missing_ok=True)
    subprocess.run(['xcrun','ar','rcs',str(archive),*[str(x) for x in objects]],env=env,check=True)
    report=dict(archive=str(archive),guest_build=a.guest_build,target=target,commands=commands,
        link_dependencies=['build/pong-smmu4k/libPongSmmu4K.a'] if a.guest_build=='22H374' else [],
        chip_id='0x06050002',gmem_bytes=0x100000,firmware_present=fw_present,gmu_firmware=gmu_fw_name,
        sqe_firmware=sqe_fw_name,zap=zap_name,
        hfi_table_words=210,hfi_queue_offset_bytes=848,active_queues=3,
        cpu_page_bytes=4096,registers=values,automatic_boot_entry=False,
        startup_boot_argument='pong_adreno_start=1',accelerator_published=False,
        cp_ring_compiled=True,cp_board_connected=True,cp_init='SM8250/SM8475 bootstrap drain, PAS13/ZAP, nonsecure timestamp fence',
        native_user_client='NativeAdrenoGpuPublished',metal_publication='NativeAdrenoPublish on matching '+a.guest_build+' IOAcceleratorES',
        metal_parent_abi=abi,metal_parent_bindings=parent_bindings,
        timestamp_gmu_offsets=[0x16220,0x16224],
        gpu_streams=[0,0x400,1,0x401],gmu_streams=[5,0x405],
        smmu_physical='0x03da0000',smmu_context_limits=[21,24],
        boot_link_status='controlled by CONFIG_XNU_PONG_ADRENO_PLATFORM',
        hardware_qualified=False,image_verification='skipped by request')
    (out/'build-report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(archive)

if __name__=='__main__':main()
