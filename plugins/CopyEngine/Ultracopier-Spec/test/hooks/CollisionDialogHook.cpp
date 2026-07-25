/* Test-only override of the three COLLISION dialogs -- FileExistsDialog, FolderExistsDialog and
 * FileIsSameDialog -- compiled into the TEST binary only (see binary_for() in test/lib/harness.py,
 * which appends this file to SOURCES). Exact mirror of hooks/FileErrorDialogHook.cpp: it lives
 * entirely under test/, the SHIPPING binary never compiles it, so each dialog's overrideFactory
 * stays nullptr there and the real GUI dialog runs unchanged.
 *
 * WHY THIS EXISTS: fileCollision=0 and folderCollision=0 ("Ask") are the SHIPPING DEFAULTS
 * (CopyEngineFactory.cpp). Before this hook, a real collision under those defaults popped a modal
 * dialog that nothing could answer, so a headless run WEDGED -- which is exactly why no case ever
 * drove the default policy and the most common real-world scenario (copying onto files that already
 * exist) had zero automated coverage. The rule is now: EVERY Ask dialog is caught and answered, so
 * a case can never hang on one; an unexpected Ask surfaces as a loud content mismatch (the default
 * answer is Skip) plus an optional marker line, never as a timeout.
 *
 * The answers are scripted by the Python case through env vars:
 *     ULTRACOPIER_TEST_FILE_COLLISION_ACTION    skip|overwrite|rename|cancel|
 *                                               ow_if_newer|ow_if_older|ow_if_not_same_size|
 *                                               ow_if_not_same_mdate|ow_if_not_same_size_and_date
 *     ULTRACOPIER_TEST_FOLDER_COLLISION_ACTION  merge|skip|rename|cancel
 *     ULTRACOPIER_TEST_SAME_FILE_ACTION         skip|overwrite|rename|cancel   (FileIsSameDialog)
 *     ULTRACOPIER_TEST_COLLISION_ALWAYS         1 -> tick "always do this" (apply to every later one)
 *     ULTRACOPIER_TEST_COLLISION_RENAME         explicit new name for the rename answers
 *     ULTRACOPIER_TEST_COLLISION_MARKER         path: append one line per dialog CREATED, so a case
 *                                               can PROVE the Ask path was really taken
 * Unset -> Skip (the safe, non-hanging, non-destructive answer).
 */
#include "../../FileExistsDialog.h"
#include "../../FolderExistsDialog.h"
#include "../../FileIsSameDialog.h"
#include "../../DiskSpace.h"

#include <QDialog>
#include <cstdlib>
#include <cstring>
#include <cstdio>

namespace {

/// \brief append one line per dialog created, when the case asked for a marker file.
void mark(const char *kind)
{
    const char *m=getenv("ULTRACOPIER_TEST_COLLISION_MARKER");
    if(m!=NULL && m[0]!='\0')
    {
        FILE *f=fopen(m,"a");
        if(f!=NULL) { fprintf(f,"%s\n",kind); fclose(f); }
    }
}

bool envAlways()
{
    const char *e=getenv("ULTRACOPIER_TEST_COLLISION_ALWAYS");
    return (e!=NULL && e[0]=='1');
}

std::string envRename(const std::string &fallback)
{
    const char *e=getenv("ULTRACOPIER_TEST_COLLISION_RENAME");
    if(e!=NULL && e[0]!='\0')
        return std::string(e);
    return fallback;
}

/// \brief map an env word to a FileExistsAction (shared by FileExists + FileIsSame).
FileExistsAction fileActionFromEnv(const char *var)
{
    const char *e=getenv(var);
    if(e!=NULL)
    {
        if(strcmp(e,"skip")==0)                        return FileExists_Skip;
        if(strcmp(e,"overwrite")==0)                   return FileExists_Overwrite;
        if(strcmp(e,"rename")==0)                      return FileExists_Rename;
        if(strcmp(e,"cancel")==0)                      return FileExists_Cancel;
        if(strcmp(e,"ow_if_newer")==0)                 return FileExists_OverwriteIfNewer;
        if(strcmp(e,"ow_if_older")==0)                 return FileExists_OverwriteIfOlder;
        if(strcmp(e,"ow_if_not_same_size")==0)         return FileExists_OverwriteIfNotSameSize;
        if(strcmp(e,"ow_if_not_same_mdate")==0)        return FileExists_OverwriteIfNotSameMdate;
        if(strcmp(e,"ow_if_not_same_size_and_date")==0)return FileExists_OverwriteIfNotSameSizeAndDate;
    }
    // Reaching here means a case hit an Ask it did not script. Skip keeps the job COMPLETING (the
    // whole point of this hook) and shows up as a content mismatch rather than a silent pass.
    return FileExists_Skip;
}

class FileExistsDialogTest : public FileExistsDialog
{
public:
    FileExistsDialogTest(QWidget *parent, INTERNALTYPEPATH source, INTERNALTYPEPATH destination,
                         std::string firstRenamingRule, std::string otherRenamingRule,
                         FacilityInterface *facilityEngine)
        : FileExistsDialog(parent,source,destination,firstRenamingRule,otherRenamingRule,facilityEngine)
    { mark("file_collision"); }

    int exec() override { return QDialog::Accepted; }
    FileExistsAction getAction() override { return fileActionFromEnv("ULTRACOPIER_TEST_FILE_COLLISION_ACTION"); }
    bool getAlways() override { return envAlways(); }
    std::string getNewName() override { return envRename(FileExistsDialog::getNewName()); }
};

class FileIsSameDialogTest : public FileIsSameDialog
{
public:
    FileIsSameDialogTest(QWidget *parent, INTERNALTYPEPATH fileInfo, std::string firstRenamingRule,
                         std::string otherRenamingRule, FacilityInterface *facilityEngine)
        : FileIsSameDialog(parent,fileInfo,firstRenamingRule,otherRenamingRule,facilityEngine)
    { mark("same_file"); }

    int exec() override { return QDialog::Accepted; }
    FileExistsAction getAction() override { return fileActionFromEnv("ULTRACOPIER_TEST_SAME_FILE_ACTION"); }
    bool getAlways() override { return envAlways(); }
    std::string getNewName() override { return envRename(FileIsSameDialog::getNewName()); }
};

class FolderExistsDialogTest : public FolderExistsDialog
{
public:
    FolderExistsDialogTest(QWidget *parent, INTERNALTYPEPATH source, bool isSame, INTERNALTYPEPATH destination,
                           std::string firstRenamingRule, std::string otherRenamingRule)
        : FolderExistsDialog(parent,source,isSame,destination,firstRenamingRule,otherRenamingRule)
    { mark("folder_collision"); }

    int exec() override { return QDialog::Accepted; }
    FolderExistsAction getAction() override
    {
        const char *e=getenv("ULTRACOPIER_TEST_FOLDER_COLLISION_ACTION");
        if(e!=NULL)
        {
            if(strcmp(e,"merge")==0)  return FolderExists_Merge;
            if(strcmp(e,"skip")==0)   return FolderExists_Skip;
            if(strcmp(e,"rename")==0) return FolderExists_Rename;
            if(strcmp(e,"cancel")==0) return FolderExists_Cancel;
        }
        // Unscripted folder Ask -> Merge: for a FOLDER, merge is the non-destructive answer that
        // still lets the copy proceed (Skip would silently drop the whole subtree).
        return FolderExists_Merge;
    }
    bool getAlways() override { return envAlways(); }
    std::string getNewName() override { return envRename(FolderExistsDialog::getNewName()); }
};

/// \brief headless "not enough space" dialog. Default = PROCEED: the space estimate is advisory and
/// a false positive must not silently cancel a test's copy. A case wanting the cancel path sets
/// ULTRACOPIER_TEST_DISKSPACE_ACTION=cancel.
class DiskSpaceTest : public DiskSpace
{
public:
    DiskSpaceTest(FacilityInterface *facilityEngine, std::vector<Diskspace> list, QWidget *parent)
        : DiskSpace(facilityEngine,list,parent)
    { mark("disk_space"); }

    int exec() override { return QDialog::Accepted; }
    bool getAction() const override
    {
        const char *e=getenv("ULTRACOPIER_TEST_DISKSPACE_ACTION");
        if(e!=NULL && strcmp(e,"cancel")==0)
            return false;
        return true;
    }
};

DiskSpace *makeDiskSpace(FacilityInterface *facilityEngine, std::vector<Diskspace> list, QWidget *parent)
{ return new DiskSpaceTest(facilityEngine,list,parent); }

FileExistsDialog *makeFileExists(QWidget *parent, INTERNALTYPEPATH source, INTERNALTYPEPATH destination,
                                 std::string firstRenamingRule, std::string otherRenamingRule,
                                 FacilityInterface *facilityEngine)
{ return new FileExistsDialogTest(parent,source,destination,firstRenamingRule,otherRenamingRule,facilityEngine); }

FileIsSameDialog *makeFileIsSame(QWidget *parent, INTERNALTYPEPATH fileInfo, std::string firstRenamingRule,
                                 std::string otherRenamingRule, FacilityInterface *facilityEngine)
{ return new FileIsSameDialogTest(parent,fileInfo,firstRenamingRule,otherRenamingRule,facilityEngine); }

FolderExistsDialog *makeFolderExists(QWidget *parent, INTERNALTYPEPATH source, bool isSame, INTERNALTYPEPATH destination,
                                     std::string firstRenamingRule, std::string otherRenamingRule)
{ return new FolderExistsDialogTest(parent,source,isSame,destination,firstRenamingRule,otherRenamingRule); }

/// \brief install the overrides at static-init time (the .o is linked into the test binary only).
struct CollisionDialogHookInstaller
{
    CollisionDialogHookInstaller()
    {
        FileExistsDialog::overrideFactory=&makeFileExists;
        FileIsSameDialog::overrideFactory=&makeFileIsSame;
        FolderExistsDialog::overrideFactory=&makeFolderExists;
        DiskSpace::overrideFactory=&makeDiskSpace;
    }
};
static CollisionDialogHookInstaller s_collisionDialogHookInstaller;

} // namespace
